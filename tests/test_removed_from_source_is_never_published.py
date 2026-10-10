"""A row that left its source's file reaches no public or paid surface (docs/51 §2.7 item 1).

The loader stores such a removal as `removed_from_source` unless the source declares that a
disappearance means withdrawal (`services/ingest/test_loader_removals.py`). This file loads one
through the real loader, beside an ordinary public `status_change` on the same source as a control,
and walks every surface a beta reader or a channel could meet it on: the public and Pro event list,
event detail and record history, the RSS/JSON feeds, saved-search alert evaluation and the private
feed, webhook enqueue, the social draft tick and its bridge, and the CRM signal mapping. It then
gives the removal publication timestamps, as a backfill might, and checks that the type alone still
keeps it off every one of them (`services.db.models.NON_PUBLIC_EVENT_TYPES`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.alerts import webhooks
from services.alerts.evaluate import evaluate_saved_search
from services.alerts.feed import matching_items_for_feed
from services.api.interconnection_points import RECENT_CHANGE_EVENT_TYPES
from services.crm.signals import signal_from_event
from services.db.models import (
    NON_PUBLIC_EVENT_TYPES,
    REMOVED_FROM_SOURCE_EVENT_TYPE,
    Event,
    Post,
    Proposal,
    SavedSearch,
    WebhookDelivery,
    WebhookEndpoint,
)
from services.ids import public_id
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import sample_proposal_row
from services.social.db_events import social_event_from_db
from services.social.worker import draft_posts_tick
from tests.conftest import make_account, make_api_key, make_user
from tests.test_publication_is_never_time_delayed import iso_source_entry

UTC = dt.UTC
SRC = "us.iso.test.gen_queue"


def _row(record_id: str, lifecycle_state: str = "filed") -> dict[str, Any]:
    r = dict(sample_proposal_row(record_id, lifecycle_state=lifecycle_state))
    r.update(
        record_id=f"{SRC}:{record_id}",
        source_id=SRC,
        source_url=f"https://example.org/queue/{record_id}",
        name_canonical=f"Project {record_id}",
        name_norm=f"project {record_id.lower()}",
    )
    return r


def _diff(record_id: str, event_type: str, before: Any, after: Any, at: str) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "record_id": f"{SRC}:{record_id}",
        "source_id": SRC,
        "field": "lifecycle_state",
        "before": before,
        "after": after,
        "observed_at": at,
    }


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """Accounts, a saved search and a webhook endpoint registered first; then two loads of one ISO
    register: Q1 and Q2 appear, then Q1 moves filed -> studied (the control) and Q2 leaves the file."""
    pro = make_account(db, entitlement="pro", name="Pro Capital")
    pro_user = make_user(db, pro, email="pro@example.com")
    _key, pro_secret = make_api_key(db, pro, pro_user, scopes=["read:live"])
    api = make_account(db, entitlement="api", name="Api Capital")
    api_user = make_user(db, api, email="api@example.com")
    search = SavedSearch(
        public_id="ss_removal_test",
        user_id=pro_user.id,
        account_id=pro.id,
        name="every event",
        entity="event",
        query={},
        query_hash="x",
        channels=["email"],
    )
    endpoint = WebhookEndpoint(
        public_id="wh_removal_test",
        account_id=api.id,
        created_by_user_id=api_user.id,
        url="https://example.com/hook",
        types=["event.published"],
        entity="event",
        query={},
        secret="s3cret",
    )
    db.add_all([search, endpoint])
    db.flush()

    src = upsert_licence_and_source(db, iso_source_entry(SRC), "2026-10-10")
    day1 = "2026-10-08T06:00:00Z"
    load_dataframe(
        db,
        src,
        "proposal",
        pd.DataFrame([_row("Q1"), _row("Q2")]),
        pd.DataFrame([_diff("Q1", "new", None, "filed", day1), _diff("Q2", "new", None, "filed", day1)]),
    )
    day2 = "2026-10-09T06:00:00Z"
    result = load_dataframe(
        db,
        src,
        "proposal",
        pd.DataFrame([_row("Q1", "studied")]),
        pd.DataFrame(
            [
                _diff("Q1", "status_change", "filed", "studied", day2),
                _diff("Q2", "removed", "filed", None, day2),
            ]
        ),
    )
    assert result.removals_unpublished == 1
    db.commit()
    removal = db.scalars(select(Event).where(Event.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE)).one()
    control = db.scalars(select(Event).where(Event.event_type == "status_change")).one()
    q2 = db.get(Proposal, removal.subject_id)
    assert q2 is not None
    return {
        "removal": removal,
        "control": control,
        "q2": q2,
        "search": search,
        "pro": pro,
        "pro_headers": {"Authorization": f"Bearer {pro_secret}"},
        "endpoint": endpoint,
    }


def _evt(ev: Event) -> str:
    return public_id("evt", ev.id)


def _assert_api_and_feeds_exclude(client: Any, w: dict[str, Any]) -> None:
    removal, control, q2 = w["removal"], w["control"], w["q2"]
    for headers in ({}, w["pro_headers"]):
        tier = "pro" if headers else "public"
        listing = client.get("/v1/events", headers=headers)
        assert listing.status_code == 200, listing.text
        ids = [e["id"] for e in listing.json()["data"]]
        assert _evt(control) in ids, tier
        assert _evt(removal) not in ids, tier
        assert all(e["event_type"] != REMOVED_FROM_SOURCE_EVENT_TYPE for e in listing.json()["data"])
        # asking for the type by name finds nothing either
        by_type = client.get(
            "/v1/events", params={"event_type": REMOVED_FROM_SOURCE_EVENT_TYPE}, headers=headers
        )
        assert by_type.status_code == 200 and by_type.json()["data"] == [], tier
        assert client.get(f"/v1/events/{_evt(removal)}", headers=headers).status_code == 404, tier
        assert client.get(f"/v1/events/{_evt(control)}", headers=headers).status_code == 200, tier
        # the record itself is still served (it only left the file), its history without the removal
        history = client.get(f"/v1/proposals/{q2.public_id}/events", headers=headers)
        assert history.status_code == 200, history.text
        assert [e["event_type"] for e in history.json()["data"]] == ["created"], tier
    for fmt in ("json", "rss"):
        feed = client.get(f"/feeds/events.{fmt}")
        assert feed.status_code == 200
        assert _evt(control) in feed.text, fmt
        assert _evt(removal) not in feed.text and REMOVED_FROM_SOURCE_EVENT_TYPE not in feed.text, fmt
        assert "withdrawn" not in feed.text, fmt


def _assert_alerts_webhooks_social_exclude(
    db: Session, db_sessionmaker: sessionmaker[Session], w: dict[str, Any]
) -> None:
    removal, control = w["removal"], w["control"]
    search = db.get(SavedSearch, w["search"].id)
    pro = w["pro"]
    assert search is not None
    search.watermark_seq = 0
    matched = evaluate_saved_search(db, search, pro)
    seqs = [m.event_seq for m in matched]
    assert control.seq in seqs and removal.seq not in seqs
    assert not any("withdrawn" in (m.change or "") for m in matched)
    feed_items = matching_items_for_feed(db, search, pro)
    assert REMOVED_FROM_SOURCE_EVENT_TYPE not in str(feed_items)

    endpoint = db.get(WebhookEndpoint, w["endpoint"].id)
    assert endpoint is not None
    endpoint.watermark_seq = 0
    db.flush()
    assert webhooks.enqueue_deliveries_for_event(db, removal) == []
    assert not webhooks.event_visible_for_endpoint(db, endpoint, removal)
    webhooks.enqueue_new_deliveries(db)
    delivered = set(db.scalars(select(WebhookDelivery.event_seq)))
    assert control.seq in delivered and removal.seq not in delivered
    db.rollback()

    assert social_event_from_db(db, removal) is None
    report = draft_posts_tick(db_sessionmaker)
    assert report.errors == ()
    assert db.scalars(select(Post).where(Post.event_id == removal.id)).all() == []

    assert signal_from_event(removal, subject_name="Project Q2", subject_url="https://example.org/p") is None
    assert REMOVED_FROM_SOURCE_EVENT_TYPE not in RECENT_CHANGE_EVENT_TYPES


def test_the_removal_is_stored_but_never_published(db: Session, world: dict[str, Any]) -> None:
    removal = world["removal"]
    assert removal.published_at is None and removal.public_at is None
    assert removal.after == {"removal_meaning": "unknown"}
    assert REMOVED_FROM_SOURCE_EVENT_TYPE in NON_PUBLIC_EVENT_TYPES
    q2 = db.get(Proposal, removal.subject_id)
    assert q2 is not None and q2.lifecycle_state == "filed", "a removal does not move the lifecycle"
    assert not db.scalars(select(Event).where(Event.event_type == "withdrawn")).all()


def test_no_public_or_pro_read_serves_it(client: Any, world: dict[str, Any]) -> None:
    _assert_api_and_feeds_exclude(client, world)


def test_no_alert_webhook_social_or_crm_path_takes_it(
    db: Session, db_sessionmaker: sessionmaker[Session], world: dict[str, Any]
) -> None:
    _assert_alerts_webhooks_social_exclude(db, db_sessionmaker, world)


def test_the_type_alone_keeps_it_off_every_surface(
    client: Any, db: Session, db_sessionmaker: sessionmaker[Session], world: dict[str, Any]
) -> None:
    """A backfill that stamps publication times on it (as migration 0019 did for every event with a
    `published_at`) must not surface it: the event predicate and the social bridge exclude the type
    by name."""
    removal = db.get(Event, world["removal"].id)
    assert removal is not None
    stamped = dt.datetime.now(UTC) - dt.timedelta(hours=1)
    removal.published_at = removal.public_at = stamped
    db.commit()
    world["removal"] = removal
    _assert_api_and_feeds_exclude(client, world)
    _assert_alerts_webhooks_social_exclude(db, db_sessionmaker, world)
