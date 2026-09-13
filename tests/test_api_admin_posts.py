"""Tests for `services/api/admin_posts.py`: the social review queue, channel switches, admin
API-key issuance, and the public intake/report write endpoints (Sprint 3 item 3).

`services.api.admin_posts.router` is not yet mounted on `services.api.app.app` (the coordinator
mounts it once every Sprint 3 agent's module lands); this file mounts it once at import time so
`TestClient` can exercise the routes, exactly the way `services/api/app.py` will.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import jsonschema
import yaml

from services.api.admin_posts import router as admin_posts_router
from services.api.app import app
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import ApiKey, Event, Post, Task
from tests.conftest import login, make_account, make_user

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "api" / "openapi.yaml"

if admin_posts_router not in getattr(app, "_admin_posts_mounted", []):
    app.include_router(admin_posts_router)
    app._admin_posts_mounted = [*getattr(app, "_admin_posts_mounted", []), admin_posts_router]  # type: ignore[attr-defined]

with OPENAPI_PATH.open(encoding="utf-8") as _fh:
    SPEC = yaml.safe_load(_fh)


def assert_valid(schema_name: str, instance: object) -> None:
    schema = SPEC["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{schema_name}: {e.message} at {list(e.absolute_path)}" for e in errors)


# --------------------------------------------------------------------------------------- fixtures
def _make_operator(db, *, role: str = "operator", email: str = "ops@example.com"):
    account = make_account(db, entitlement="admin", name="Ops")
    return make_user(db, account, email=email, role=role)


def _seed_event(db, *, publish_state: str = "public"):
    lic = make_open_licence(db)
    source = make_public_source(db, lic)
    proposal = make_visible_proposal(db, source)
    proposal.publish_state = publish_state
    db.flush()
    event = make_event(db, proposal, source)
    return proposal, event


def _make_post(
    db,
    event: Event,
    *,
    channel: str = "bluesky",
    state: str = "draft",
    gate_checked_at: dt.datetime | None = None,
    body: str | None = None,
    disclosure_label: str | None = None,
    scheduled_for: dt.datetime | None = None,
    reject_reason: str | None = None,
    approved_by_user_id=None,
) -> Post:
    from services.ids import public_id

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
        disclosure_label=disclosure_label,
        state=state,
        gate_checked_at=gate_checked_at or dt.datetime.now(UTC),
        scheduled_for=scheduled_for,
        reject_reason=reject_reason,
        approved_by_user_id=approved_by_user_id,
    )
    db.add(post)
    db.flush()
    post.public_id = public_id("post", post.id)
    db.flush()
    return post


# ============================================================================ post review queue
def test_admin_list_posts_requires_auth(client):
    resp = client.get("/admin/v1/posts")
    assert resp.status_code == 401


def test_admin_list_posts_happy_path_and_filters(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    _make_post(db, event, channel="bluesky", state="draft")
    _make_post(db, event, channel="x", state="approved")
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/posts")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid("PostListResponse", body)
    assert len(body["data"]) == 2
    assert body["data"][0]["created_at"] <= body["data"][1]["created_at"], "oldest draft first"

    filtered = client.get("/admin/v1/posts", params={"channel": "x"})
    assert filtered.status_code == 200
    assert [p["channel"] for p in filtered.json()["data"]] == ["x"]

    by_state = client.get("/admin/v1/posts", params={"state": "approved"})
    assert [p["state"] for p in by_state.json()["data"]] == ["approved"]


def test_admin_list_posts_rejects_unknown_query_param(client, db):
    operator = _make_operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/posts", params={"bogus": "1"})
    assert resp.status_code == 400


def test_admin_get_post_rejects_malformed_id(client, db):
    operator = _make_operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/posts/not-a-post-id")
    assert resp.status_code == 404


def test_admin_list_posts_rejects_bad_channel_and_state(client, db):
    operator = _make_operator(db)
    db.commit()
    login(client, db, operator)
    assert client.get("/admin/v1/posts", params={"channel": "mastodon"}).status_code == 400
    assert client.get("/admin/v1/posts", params={"state": "deleted"}).status_code == 400


def test_admin_list_posts_filters_by_subject_type_and_event_type(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    _make_post(db, event)
    db.commit()
    login(client, db, operator)

    by_subject = client.get("/admin/v1/posts", params={"subject_type": "proposal"})
    assert len(by_subject.json()["data"]) == 1
    by_other_subject = client.get("/admin/v1/posts", params={"subject_type": "opportunity"})
    assert by_other_subject.json()["data"] == []

    by_event_type = client.get("/admin/v1/posts", params={"event_type": event.event_type})
    assert len(by_event_type.json()["data"]) == 1
    by_other_event_type = client.get("/admin/v1/posts", params={"event_type": "created"})
    assert by_other_event_type.json()["data"] == []


def test_admin_get_post_for_an_opportunity_subject(client, db):
    from services.api.conftest import make_visible_opportunity

    operator = _make_operator(db)
    lic = make_open_licence(db)
    source = make_public_source(db, lic)
    opportunity = make_visible_opportunity(db, source)
    event = Event(
        subject_type="opportunity",
        subject_id=opportunity.id,
        event_type="status_change",
        observed_at=dt.datetime.now(UTC),
        idempotency_key="evt-opp-test-1",
    )
    db.add(event)
    db.flush()
    post = _make_post(db, event)
    post.subject_type = "opportunity"
    post.subject_id = opportunity.id
    db.flush()
    db.commit()
    login(client, db, operator)

    resp = client.get(f"/admin/v1/posts/{post.public_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["subject"]["name"] == opportunity.title


def test_admin_get_post_with_unresolvable_subject_falls_back_to_unknown(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    post.subject_type = "organization"
    db.flush()
    db.commit()
    login(client, db, operator)

    resp = client.get(f"/admin/v1/posts/{post.public_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["subject"]["name"] == "Unknown"


def test_admin_get_post_happy_path_and_404(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    db.commit()
    login(client, db, operator)

    resp = client.get(f"/admin/v1/posts/{post.public_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid("PostDetailResponse", body)
    assert body["data"]["post_id"] == post.public_id
    assert body["data"]["event"]["id"].startswith("evt_")
    assert body["data"]["subject"]["name"] == "Test Storage Project 1"

    missing = client.get("/admin/v1/posts/post_doesnotexist000000000")
    assert missing.status_code == 404


def test_admin_update_post_happy_path_and_audits(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, channel="bluesky")
    db.commit()
    login(client, db, operator)

    new_body = f"Updated: {post.body}"
    resp = client.patch(
        f"/admin/v1/posts/{post.public_id}",
        json={"body": new_body, "reason": "typo fix"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid("PostDetailResponse", body)
    assert body["data"]["body"] == new_body

    events = db.query(Event).filter_by(subject_type="post", event_type="admin_edit").all()
    assert len(events) == 1
    assert events[0].reason == "typo fix"
    assert events[0].after["body"] == new_body
    assert "body" in events[0].before


def test_admin_update_post_404(client, db):
    operator = _make_operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.patch("/admin/v1/posts/post_doesnotexist000000000", json={"reason": "r", "body": "x"})
    assert resp.status_code == 404


def test_admin_update_post_requires_at_least_one_change(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    db.commit()
    login(client, db, operator)
    resp = client.patch(f"/admin/v1/posts/{post.public_id}", json={"reason": "just because"})
    assert resp.status_code == 400


def test_admin_update_post_rejects_non_string_or_blank_body(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    db.commit()
    login(client, db, operator)
    resp = client.patch(f"/admin/v1/posts/{post.public_id}", json={"body": "   ", "reason": "r"})
    assert resp.status_code == 400


def test_admin_update_post_rejects_removing_disclosure_label(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, disclosure_label="Automated post — see disclosure")
    post.body = f"{post.body} Automated post — see disclosure"
    db.commit()
    login(client, db, operator)
    resp = client.patch(
        f"/admin/v1/posts/{post.public_id}",
        json={"body": f"{post.credit_line} {post.link_url}", "reason": "r"},
    )
    assert resp.status_code == 400


def test_admin_update_post_can_set_and_clear_scheduled_for(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    db.commit()
    login(client, db, operator)

    when = (dt.datetime.now(UTC) + dt.timedelta(days=1)).isoformat()
    resp = client.patch(
        f"/admin/v1/posts/{post.public_id}", json={"scheduled_for": when, "reason": "pencil in"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["scheduled_for"] is not None

    cleared = client.patch(
        f"/admin/v1/posts/{post.public_id}", json={"scheduled_for": None, "reason": "unschedule"}
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["scheduled_for"] is None


def test_admin_update_post_rejects_bad_scheduled_for_format(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    db.commit()
    login(client, db, operator)
    resp = client.patch(
        f"/admin/v1/posts/{post.public_id}", json={"scheduled_for": "not-a-date", "reason": "r"}
    )
    assert resp.status_code == 400


def test_admin_update_post_requires_reason(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    db.commit()
    login(client, db, operator)
    resp = client.patch(f"/admin/v1/posts/{post.public_id}", json={"body": "x" * 10})
    assert resp.status_code == 400


def test_admin_update_post_rejects_body_over_channel_limit(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, channel="x")
    db.commit()
    login(client, db, operator)
    too_long = f"{post.credit_line} {post.link_url} " + ("y" * 300)
    resp = client.patch(
        f"/admin/v1/posts/{post.public_id}", json={"body": too_long, "reason": "too long test"}
    )
    assert resp.status_code == 400


def test_admin_update_post_rejects_removing_the_link(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event)
    db.commit()
    login(client, db, operator)
    resp = client.patch(
        f"/admin/v1/posts/{post.public_id}", json={"body": "no link here at all", "reason": "oops"}
    )
    assert resp.status_code == 400


def test_admin_update_post_conflict_when_not_editable(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="published")
    db.commit()
    login(client, db, operator)
    resp = client.patch(f"/admin/v1/posts/{post.public_id}", json={"body": f"{post.body} x", "reason": "r"})
    assert resp.status_code == 409


def test_admin_approve_post_happy_path(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="draft")
    db.commit()
    login(client, db, operator)

    resp = client.post(f"/admin/v1/posts/{post.public_id}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid("PostDetailResponse", body)
    assert body["data"]["state"] == "approved"
    assert body["data"]["approved_by_user_id"] == operator.public_id

    events = db.query(Event).filter_by(subject_type="post", event_type="admin_edit").all()
    assert len(events) == 1
    assert events[0].after["state"] == "approved"


def test_admin_approve_post_404(client, db):
    operator = _make_operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post("/admin/v1/posts/post_doesnotexist000000000/approve")
    assert resp.status_code == 404


def test_admin_approve_post_conflict_when_not_draft(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="approved")
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/posts/{post.public_id}/approve")
    assert resp.status_code == 409


def test_admin_approve_post_gate_unmet_when_subject_not_public(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db, publish_state="pending_review")
    post = _make_post(db, event, state="draft")
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/posts/{post.public_id}/approve")
    assert resp.status_code == 422
    assert resp.json()["code"] == "gate_unmet"


def test_admin_approve_post_gate_unmet_when_gate_check_stale(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    stale = dt.datetime.now(UTC) - dt.timedelta(hours=25)
    post = _make_post(db, event, state="draft", gate_checked_at=stale)
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/posts/{post.public_id}/approve")
    assert resp.status_code == 422
    assert resp.json()["code"] == "gate_unmet"


def test_admin_reject_post_happy_path(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="approved")
    db.commit()
    login(client, db, operator)

    resp = client.post(f"/admin/v1/posts/{post.public_id}/reject", json={"reason": "wrong_fact"})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid("PostDetailResponse", body)
    assert body["data"]["state"] == "rejected"
    assert body["data"]["reject_reason"] == "wrong_fact"

    events = db.query(Event).filter_by(subject_type="post", event_type="admin_edit").all()
    assert events[0].after["reject_reason"] == "wrong_fact"


def test_admin_reject_post_404(client, db):
    operator = _make_operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post("/admin/v1/posts/post_doesnotexist000000000/reject", json={"reason": "style"})
    assert resp.status_code == 404


def test_admin_reject_post_rejects_unknown_reason_code(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="draft")
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/posts/{post.public_id}/reject", json={"reason": "not_a_code"})
    assert resp.status_code == 400


def test_admin_reject_post_conflict_when_terminal(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="published")
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/posts/{post.public_id}/reject", json={"reason": "style"})
    assert resp.status_code == 409


def test_admin_schedule_post_happy_path(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="approved")
    db.commit()
    login(client, db, operator)

    when = (dt.datetime.now(UTC) + dt.timedelta(hours=2)).isoformat()
    resp = client.post(
        f"/admin/v1/posts/{post.public_id}/schedule",
        json={"scheduled_for": when, "reason": "slot open"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid("PostDetailResponse", body)
    assert body["data"]["state"] == "scheduled"
    assert body["data"]["scheduled_for"] is not None


def test_admin_schedule_post_404(client, db):
    operator = _make_operator(db)
    db.commit()
    login(client, db, operator)
    when = (dt.datetime.now(UTC) + dt.timedelta(hours=2)).isoformat()
    resp = client.post(
        "/admin/v1/posts/post_doesnotexist000000000/schedule",
        json={"scheduled_for": when, "reason": "r"},
    )
    assert resp.status_code == 404


def test_admin_schedule_post_rejects_bad_datetime_format(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="approved")
    db.commit()
    login(client, db, operator)
    resp = client.post(
        f"/admin/v1/posts/{post.public_id}/schedule",
        json={"scheduled_for": "not-a-date", "reason": "r"},
    )
    assert resp.status_code == 400


def test_admin_schedule_post_requires_reason(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="approved")
    db.commit()
    login(client, db, operator)
    when = (dt.datetime.now(UTC) + dt.timedelta(hours=2)).isoformat()
    resp = client.post(f"/admin/v1/posts/{post.public_id}/schedule", json={"scheduled_for": when})
    assert resp.status_code == 400


def test_admin_schedule_post_requires_future_time(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="approved")
    db.commit()
    login(client, db, operator)
    past = (dt.datetime.now(UTC) - dt.timedelta(hours=1)).isoformat()
    resp = client.post(
        f"/admin/v1/posts/{post.public_id}/schedule", json={"scheduled_for": past, "reason": "r"}
    )
    assert resp.status_code == 400


def test_admin_schedule_post_requires_scheduled_for(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="approved")
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/posts/{post.public_id}/schedule", json={"reason": "r"})
    assert resp.status_code == 400


def test_admin_schedule_post_conflict_when_not_approved(client, db):
    operator = _make_operator(db)
    _proposal, event = _seed_event(db)
    post = _make_post(db, event, state="draft")
    db.commit()
    login(client, db, operator)
    when = (dt.datetime.now(UTC) + dt.timedelta(hours=2)).isoformat()
    resp = client.post(
        f"/admin/v1/posts/{post.public_id}/schedule", json={"scheduled_for": when, "reason": "r"}
    )
    assert resp.status_code == 409


# ============================================================================== channel switches
def test_admin_set_channel_auto_publish_requires_owner(client, db):
    operator = _make_operator(db, role="operator", email="op2@example.com")
    db.commit()
    login(client, db, operator)
    resp = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={"auto_publish": True, "disclosure_label_confirmed": True, "reason": "graduate"},
    )
    assert resp.status_code == 403


def test_admin_set_channel_auto_publish_happy_path(client, db):
    owner = _make_operator(db, role="owner", email="owner1@example.com")
    db.commit()
    login(client, db, owner)

    resp = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={
            "auto_publish": True,
            "disclosure_label_confirmed": True,
            "daily_cap": 20,
            "reason": "graduated after 200 posts",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid("ChannelConfigResponse", body)
    assert body["data"]["auto_publish"] is True
    assert body["data"]["review_required"] is False
    assert body["data"]["daily_cap"] == 20
    assert body["data"]["disclosure_label"]
    assert body["data"]["event_id"].startswith("evt_")

    events = db.query(Event).filter_by(subject_type="channel_config", event_type="admin_edit").all()
    assert len(events) == 1
    assert events[0].after["auto_publish"] is True


def test_admin_set_channel_auto_publish_requires_boolean(client, db):
    owner = _make_operator(db, role="owner", email="owner-bool@example.com")
    db.commit()
    login(client, db, owner)
    resp = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={"auto_publish": "yes", "reason": "r"},
    )
    assert resp.status_code == 400


def test_admin_set_channel_auto_publish_updates_existing_row(client, db):
    owner = _make_operator(db, role="owner", email="owner-twice@example.com")
    db.commit()
    login(client, db, owner)

    first = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={
            "auto_publish": True,
            "disclosure_label_confirmed": True,
            "daily_cap": 10,
            "reason": "enable",
        },
    )
    assert first.status_code == 200

    second = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={"auto_publish": False, "reason": "pause after an incident"},
    )
    assert second.status_code == 200
    body = second.json()["data"]
    assert body["auto_publish"] is False
    assert body["review_required"] is True
    assert body["disclosure_label"] is None

    events = (
        db.query(Event)
        .filter_by(subject_type="channel_config", event_type="admin_edit")
        .order_by(Event.seq)
        .all()
    )
    assert len(events) == 2
    assert events[1].before["auto_publish"] is True


def test_admin_set_channel_auto_publish_gate_unmet_without_disclosure(client, db):
    owner = _make_operator(db, role="owner", email="owner2@example.com")
    db.commit()
    login(client, db, owner)
    resp = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={"auto_publish": True, "reason": "graduate"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "gate_unmet"


def test_admin_set_channel_auto_publish_requires_reason(client, db):
    owner = _make_operator(db, role="owner", email="owner3@example.com")
    db.commit()
    login(client, db, owner)
    resp = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={"auto_publish": False},
    )
    assert resp.status_code == 400


def test_admin_set_channel_auto_publish_unknown_channel_is_404(client, db):
    owner = _make_operator(db, role="owner", email="owner4@example.com")
    db.commit()
    login(client, db, owner)
    resp = client.put(
        "/admin/v1/channels/mastodon/auto-publish",
        json={"auto_publish": False, "reason": "r"},
    )
    assert resp.status_code == 404


# ===================================================================================== admin keys
def test_admin_create_key_happy_path_shows_secret_once(client, db):
    operator = _make_operator(db, email="ops-keys@example.com")
    customer_account = make_account(db, entitlement="api", name="Customer Co")
    db.commit()
    login(client, db, operator)

    resp = client.post(
        "/admin/v1/keys",
        json={
            "name": "customer-etl",
            "licence_accepted_version": "api-licence-1.0",
            "account_id": customer_account.public_id,
            "licence_acceptance_ref": "contract-123",
            "reason": "customer requested a key",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert_valid("ApiKeyCreatedResponse", body)
    secret = body["data"]["secret"]
    assert secret.startswith("bk_live_")

    stored = db.query(ApiKey).filter_by(public_id=body["data"]["key_id"]).one()
    assert stored.key_hash != secret
    import hashlib

    assert stored.key_hash == hashlib.sha256(secret.encode()).hexdigest()

    events = db.query(Event).filter_by(subject_type="api_key", event_type="key_issued").all()
    assert len(events) == 1
    assert events[0].reason == "customer requested a key"

    listing = client.get("/admin/v1/keys").json()
    assert_valid("ApiKeyListResponse", listing)
    assert all("secret" not in row for row in listing["data"])


def test_admin_list_keys_filters_by_account_and_revoked(client, db):
    operator = _make_operator(db, email="ops-keys-list@example.com")
    account_a = make_account(db, entitlement="api", name="Account A")
    account_b = make_account(db, entitlement="api", name="Account B")
    db.commit()
    login(client, db, operator)

    key_a = client.post(
        "/admin/v1/keys",
        json={
            "name": "a",
            "licence_accepted_version": "api-licence-1.0",
            "account_id": account_a.public_id,
            "licence_acceptance_ref": "ref-a",
            "reason": "r",
        },
    ).json()["data"]
    client.post(
        "/admin/v1/keys",
        json={
            "name": "b",
            "licence_accepted_version": "api-licence-1.0",
            "account_id": account_b.public_id,
            "licence_acceptance_ref": "ref-b",
            "reason": "r",
        },
    )

    only_a = client.get("/admin/v1/keys", params={"account_id": account_a.public_id})
    assert [k["name"] for k in only_a.json()["data"]] == ["a"]

    unknown_account = client.get("/admin/v1/keys", params={"account_id": "acc_doesnotexist0000000000"})
    assert unknown_account.status_code == 404

    client.request("DELETE", f"/admin/v1/keys/{key_a['key_id']}", json={"reason": "cleanup"})
    revoked = client.get("/admin/v1/keys", params={"revoked": "true"})
    assert [k["name"] for k in revoked.json()["data"]] == ["a"]
    active = client.get("/admin/v1/keys")
    assert [k["name"] for k in active.json()["data"]] == ["b"]


def test_admin_create_key_rejects_admin_star_scope(client, db):
    operator = _make_operator(db, email="ops-keys-scope@example.com")
    customer_account = make_account(db, entitlement="admin", name="Scope Co")
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/keys",
        json={
            "name": "n",
            "licence_accepted_version": "api-licence-1.0",
            "account_id": customer_account.public_id,
            "licence_acceptance_ref": "ref",
            "reason": "r",
            "scopes": ["admin:*"],
        },
    )
    assert resp.status_code == 400


def test_admin_create_key_honours_explicit_rate_limit(client, db):
    operator = _make_operator(db, email="ops-keys-rl@example.com")
    customer_account = make_account(db, entitlement="api", name="RL Co")
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/keys",
        json={
            "name": "n",
            "licence_accepted_version": "api-licence-1.0",
            "account_id": customer_account.public_id,
            "licence_acceptance_ref": "ref",
            "reason": "r",
            "rate_limit_per_hour": 12345,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["rate_limit_per_hour"] == 12345


def test_admin_create_key_rejects_wrong_licence_version(client, db):
    operator = _make_operator(db, email="ops-keys2@example.com")
    customer_account = make_account(db, entitlement="api", name="Customer Co 2")
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/keys",
        json={
            "name": "n",
            "licence_accepted_version": "old-version",
            "account_id": customer_account.public_id,
            "licence_acceptance_ref": "ref",
            "reason": "r",
        },
    )
    assert resp.status_code == 400


def test_admin_create_key_unknown_account_is_404(client, db):
    operator = _make_operator(db, email="ops-keys3@example.com")
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/keys",
        json={
            "name": "n",
            "licence_accepted_version": "api-licence-1.0",
            "account_id": "acc_doesnotexist0000000000",
            "licence_acceptance_ref": "ref",
            "reason": "r",
        },
    )
    assert resp.status_code == 404


def test_admin_revoke_key_happy_path_and_requires_reason(client, db):
    operator = _make_operator(db, email="ops-keys4@example.com")
    customer_account = make_account(db, entitlement="api", name="Customer Co 3")
    customer_user = make_user(db, customer_account, email="cust@example.com")
    db.commit()
    login(client, db, operator)

    created = client.post(
        "/admin/v1/keys",
        json={
            "name": "n",
            "licence_accepted_version": "api-licence-1.0",
            "account_id": customer_account.public_id,
            "licence_acceptance_ref": "ref",
            "reason": "issue",
        },
    ).json()["data"]

    missing_reason = client.request("DELETE", f"/admin/v1/keys/{created['key_id']}", json={})
    assert missing_reason.status_code == 400

    resp = client.request(
        "DELETE", f"/admin/v1/keys/{created['key_id']}", json={"reason": "customer offboarded"}
    )
    assert resp.status_code == 204

    stored = db.query(ApiKey).filter_by(public_id=created["key_id"]).one()
    assert stored.revoked_at is not None
    events = db.query(Event).filter_by(subject_type="api_key", event_type="key_revoked").all()
    assert events[0].reason == "customer offboarded"
    del customer_user  # only needed to exist for the account's created_by_user_id FK path


def test_admin_revoke_key_unknown_key_is_404(client, db):
    operator = _make_operator(db, email="ops-keys5@example.com")
    db.commit()
    login(client, db, operator)
    resp = client.request("DELETE", "/admin/v1/keys/key_doesnotexist00000000", json={"reason": "r"})
    assert resp.status_code == 404


# ============================================================================ public intake/report
_VALID_PROPOSAL_BODY = {
    "project_name": "Sunrise Storage",
    "kind": "storage",
    "jurisdiction": "US-TX",
    "lifecycle_state": "announced",
    "sponsor_name": "Sunrise Power LLC",
    "capacity_mw": 100,
    "contact": {"name": "Jamie Rivera", "email": "jamie@example.com"},
    "consent": True,
    "captcha_token": "test-token",
}

_VALID_OPPORTUNITY_BODY = {
    "title": "Solar RFP 2027",
    "kind": "rfp",
    "issuer_name": "Test Utility",
    "jurisdiction": "US-AZ",
    "technologies": ["solar_pv"],
    "url": "https://example.org/rfp/2027",
    "contact": {"name": "Sam Lee", "email": "sam@example.com"},
    "consent": True,
    "captcha_token": "test-token",
}


def test_submit_intake_proposal_happy_path_and_replay(client, db):
    resp = client.post(
        "/v1/intake/proposals",
        json=_VALID_PROPOSAL_BODY,
        headers={"Idempotency-Key": "idem-prop-1"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert_valid("IntakeAcceptedResponse", body)
    task_id = body["task_id"]

    task = db.query(Task).filter_by(public_id=task_id).one()
    assert task.type == "intake_proposal"
    assert task.pending_record["project_name"] == "Sunrise Storage"
    assert task.contact == {"name": "Jamie Rivera", "email": "jamie@example.com"}

    replay = client.post(
        "/v1/intake/proposals",
        json=_VALID_PROPOSAL_BODY,
        headers={"Idempotency-Key": "idem-prop-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["task_id"] == task_id
    assert db.query(Task).filter_by(type="intake_proposal").count() == 1


def test_submit_intake_proposal_idempotency_conflict_on_different_body(client, db):
    client.post("/v1/intake/proposals", json=_VALID_PROPOSAL_BODY, headers={"Idempotency-Key": "idem-prop-2"})
    different = {**_VALID_PROPOSAL_BODY, "project_name": "Different Project"}
    resp = client.post("/v1/intake/proposals", json=different, headers={"Idempotency-Key": "idem-prop-2"})
    assert resp.status_code == 409


def test_submit_intake_proposal_rejects_missing_field(client, db):
    bad = {**_VALID_PROPOSAL_BODY}
    del bad["sponsor_name"]
    resp = client.post("/v1/intake/proposals", json=bad)
    assert resp.status_code == 400


def test_submit_intake_proposal_validation_branches_part_one(client, db):
    for override in ({"kind": "not_a_kind"}, {"lifecycle_state": "not_a_state"}, {"jurisdiction": "texas"}):
        bad = {**_VALID_PROPOSAL_BODY, **override}
        resp = client.post("/v1/intake/proposals", json=bad)
        assert resp.status_code == 400, override


def test_submit_intake_proposal_validation_branches_part_two(client, db):
    for override in ({"consent": False}, {"captcha_token": ""}, {"description": "x" * 2001}):
        bad = {**_VALID_PROPOSAL_BODY, **override}
        resp = client.post("/v1/intake/proposals", json=bad)
        assert resp.status_code == 400, override


def test_submit_intake_proposal_rejects_bad_contact(client, db):
    missing_email = {**_VALID_PROPOSAL_BODY, "contact": {"name": "No Email"}}
    assert client.post("/v1/intake/proposals", json=missing_email).status_code == 400
    bad_email = {**_VALID_PROPOSAL_BODY, "contact": {"name": "Bad Email", "email": "not-an-email"}}
    assert client.post("/v1/intake/proposals", json=bad_email).status_code == 400


def test_submit_intake_proposal_honeypot_refuses(client, db):
    trapped = {**_VALID_PROPOSAL_BODY, "website": "http://spam.example"}
    resp = client.post("/v1/intake/proposals", json=trapped)
    assert resp.status_code == 400


def test_submit_intake_proposal_rate_limited_after_five(client, db):
    for _ in range(5):
        resp = client.post("/v1/intake/proposals", json=_VALID_PROPOSAL_BODY)
        assert resp.status_code == 202
    sixth = client.post("/v1/intake/proposals", json=_VALID_PROPOSAL_BODY)
    assert sixth.status_code == 429


def test_submit_intake_opportunity_happy_path(client, db):
    resp = client.post("/v1/intake/opportunities", json=_VALID_OPPORTUNITY_BODY)
    assert resp.status_code == 202
    body = resp.json()
    assert_valid("IntakeAcceptedResponse", body)
    task = db.query(Task).filter_by(public_id=body["task_id"]).one()
    assert task.type == "intake_opportunity"
    assert task.pending_record["title"] == "Solar RFP 2027"


def test_submit_intake_opportunity_validation_branches_part_one(client, db):
    for override in (
        {"kind": "not_a_kind"},
        {"technologies": []},
        {"budget_currency": "usd"},
    ):
        bad = {**_VALID_OPPORTUNITY_BODY, **override}
        resp = client.post("/v1/intake/opportunities", json=bad)
        assert resp.status_code == 400, override


def test_submit_intake_opportunity_validation_branches_part_two(client, db):
    for override in (
        {"url": "not-a-url"},
        {"consent": False},
        {"captcha_token": ""},
        {"summary": "x" * 2001},
    ):
        bad = {**_VALID_OPPORTUNITY_BODY, **override}
        resp = client.post("/v1/intake/opportunities", json=bad)
        assert resp.status_code == 400, override


def test_submit_intake_opportunity_replays_on_same_idempotency_key(client, db):
    first = client.post(
        "/v1/intake/opportunities", json=_VALID_OPPORTUNITY_BODY, headers={"Idempotency-Key": "idem-opp-0"}
    )
    second = client.post(
        "/v1/intake/opportunities", json=_VALID_OPPORTUNITY_BODY, headers={"Idempotency-Key": "idem-opp-0"}
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert db.query(Task).filter_by(type="intake_opportunity").count() == 1


def test_submit_intake_opportunity_idempotency_conflict_on_different_body(client, db):
    client.post(
        "/v1/intake/opportunities", json=_VALID_OPPORTUNITY_BODY, headers={"Idempotency-Key": "idem-opp-1"}
    )
    different = {**_VALID_OPPORTUNITY_BODY, "title": "A Different RFP"}
    resp = client.post("/v1/intake/opportunities", json=different, headers={"Idempotency-Key": "idem-opp-1"})
    assert resp.status_code == 409


def test_create_report_happy_path(client, db):
    _proposal, _event = _seed_event(db)
    db.commit()

    resp = client.post(
        "/v1/reports",
        json={
            "public_id": _proposal.public_id,
            "issue_type": "wrong_status",
            "description": "This project was withdrawn last month.",
            "email": "reporter@example.com",
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert_valid("ReportAcceptedResponse", body)
    task = db.query(Task).filter_by(public_id=body["task_id"]).one()
    assert task.type == "report"
    assert task.subject_type == "proposal"
    assert task.issue_type == "wrong_status"
    assert task.contact == {"email": "reporter@example.com"}


def test_create_report_resolves_opportunity_and_organization_subjects(client, db):
    from services.api.conftest import make_org, make_visible_opportunity

    lic = make_open_licence(db)
    source = make_public_source(db, lic)
    opportunity = make_visible_opportunity(db, source)
    org = make_org(db)
    db.commit()

    opp_resp = client.post(
        "/v1/reports",
        json={"public_id": opportunity.public_id, "issue_type": "other", "description": "stale listing"},
    )
    assert opp_resp.status_code == 202
    opp_task = db.query(Task).filter_by(public_id=opp_resp.json()["task_id"]).one()
    assert opp_task.subject_type == "opportunity"

    org_resp = client.post(
        "/v1/reports",
        json={"public_id": org.public_id, "issue_type": "wrong_sponsor", "description": "wrong org"},
    )
    assert org_resp.status_code == 202
    org_task = db.query(Task).filter_by(public_id=org_resp.json()["task_id"]).one()
    assert org_task.subject_type == "organization"


def test_create_report_validation_branches(client, db):
    _proposal, _event = _seed_event(db)
    db.commit()
    bad_issue = {
        "public_id": _proposal.public_id,
        "issue_type": "not_a_type",
        "description": "x",
    }
    assert client.post("/v1/reports", json=bad_issue).status_code == 400

    missing_description = {"public_id": _proposal.public_id, "issue_type": "other", "description": ""}
    assert client.post("/v1/reports", json=missing_description).status_code == 400

    bad_email = {
        "public_id": _proposal.public_id,
        "issue_type": "other",
        "description": "x",
        "email": "not-an-email",
    }
    assert client.post("/v1/reports", json=bad_email).status_code == 400


def test_create_report_idempotency_conflict_on_different_body(client, db):
    _proposal, _event = _seed_event(db)
    db.commit()
    payload = {"public_id": _proposal.public_id, "issue_type": "other", "description": "first"}
    client.post("/v1/reports", json=payload, headers={"Idempotency-Key": "idem-report-2"})
    different = {**payload, "description": "second, different text"}
    resp = client.post("/v1/reports", json=different, headers={"Idempotency-Key": "idem-report-2"})
    assert resp.status_code == 409


def test_create_report_unknown_record_is_404(client, db):
    resp = client.post(
        "/v1/reports",
        json={
            "public_id": "prop_doesnotexist0000000000",
            "issue_type": "other",
            "description": "not a real record",
        },
    )
    assert resp.status_code == 404


def test_create_report_unrecognised_id_prefix_is_404(client, db):
    resp = client.post(
        "/v1/reports",
        json={
            "public_id": "evt_00000000000",
            "issue_type": "other",
            "description": "not a record kind we handle",
        },
    )
    assert resp.status_code == 404


def test_create_report_replays_on_same_idempotency_key(client, db):
    _proposal, _event = _seed_event(db)
    db.commit()
    payload = {
        "public_id": _proposal.public_id,
        "issue_type": "wrong_sponsor",
        "description": "Sponsor is listed incorrectly.",
    }
    first = client.post("/v1/reports", json=payload, headers={"Idempotency-Key": "idem-report-1"})
    second = client.post("/v1/reports", json=payload, headers={"Idempotency-Key": "idem-report-1"})
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert db.query(Task).filter_by(type="report").count() == 1
