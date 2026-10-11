"""A record that left a full-register queue is public news, alertable, and never a social draft
(owner decision 2026-10-10).

The loader writes the departure as `delisted` for the four queues whose connectors announce removals
(`services/ingest/test_loader_delisted.py`). This file loads one through the real loader on ERCOT's
own manifest entry and declarations, beside an ordinary `status_change` on the same register as a
control, and walks every surface: the public and Pro event list, event detail and record history,
the RSS/JSON feeds, alert evaluation for a followed record, a followed sponsor and a saved search,
the digest it renders and the private feed, webhook enqueue and delivery. Each serves it worded
"No longer in ERCOT's report (reason not stated)", never "withdrawn". Then the one surface it never
reaches: the social draft tick and its bridge, which refuse it by name even when a type map names
it. The CRM lead-signal mapping takes no departure either.

Its counterpart for the non-public `removed_from_source` is
`tests/test_removed_from_source_is_never_published.py`.
"""

from __future__ import annotations

import html
import json
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pipeline.connectors.registry import Registry
from services.alerts import webhooks
from services.alerts.evaluate import evaluate_saved_search, render_digest_body
from services.alerts.feed import matching_items_for_feed
from services.crm.signals import signal_from_event
from services.db.models import (
    DELISTED_EVENT_TYPE,
    NON_PUBLIC_EVENT_TYPES,
    NON_SOCIAL_EVENT_TYPES,
    Event,
    Organization,
    Post,
    Proposal,
    SavedSearch,
    WebhookDelivery,
    WebhookEndpoint,
)
from services.ids import public_id
from services.ingest.loader import (
    connector_announces_removals,
    connector_register_name,
    connector_removal_meaning,
    load_dataframe,
    upsert_licence_and_source,
)
from services.ingest.test_loader import sample_proposal_row
from services.social import db_events
from services.social.db_events import social_event_from_db
from services.social.worker import draft_posts_tick
from tests.conftest import make_account, make_api_key, make_user

SRC = "us.iso.ercot.gen_queue"
SENTENCE = "No longer in ERCOT's report (reason not stated)"


def _row(record_id: str, lifecycle_state: str = "filed") -> dict[str, Any]:
    r = dict(sample_proposal_row(record_id, lifecycle_state=lifecycle_state))
    r.update(
        record_id=f"{SRC}:{record_id}",
        source_id=SRC,
        source_url=f"https://www.ercot.com/gis/{record_id}",
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


def _search(user: Any, account: Any, name: str, entity: str, query: dict[str, Any]) -> SavedSearch:
    return SavedSearch(
        public_id=f"ss_{name}",
        user_id=user.id,
        account_id=account.id,
        name=name,
        entity=entity,
        query=query,
        query_hash=name,
        channels=["email"],
    )


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """Two loads of ERCOT with its own declarations: Q1 and Q2 appear, then Q1 moves filed ->
    studied (the control) and Q2 leaves the report."""
    pro = make_account(db, entitlement="pro", name="Pro Capital")
    pro_user = make_user(db, pro, email="pro@example.com")
    _key, pro_secret = make_api_key(db, pro, pro_user, scopes=["read:live"])
    api = make_account(db, entitlement="api", name="Api Capital")
    api_user = make_user(db, api, email="api@example.com")
    endpoint = WebhookEndpoint(
        public_id="wh_delisted_test",
        account_id=api.id,
        created_by_user_id=api_user.id,
        url="https://example.com/hook",
        types=["event.published"],
        entity="event",
        query={},
        secret="s3cret",
    )
    db.add(endpoint)
    db.flush()

    registry = Registry()
    declared: dict[str, Any] = {
        "removal_meaning": connector_removal_meaning(registry, SRC),
        "announce_removals": connector_announces_removals(registry, SRC),
        "register_name": connector_register_name(registry, SRC),
    }
    assert declared == {"removal_meaning": "unknown", "announce_removals": True, "register_name": "ERCOT"}
    src = upsert_licence_and_source(db, registry.get(SRC), registry.version)
    day1 = "2026-10-08T06:00:00Z"
    load_dataframe(
        db,
        src,
        "proposal",
        pd.DataFrame([_row("Q1"), _row("Q2")]),
        pd.DataFrame([_diff("Q1", "new", None, "filed", day1), _diff("Q2", "new", None, "filed", day1)]),
        **declared,
    )
    day1_head = max(db.scalars(select(Event.seq)))
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
        **declared,
    )
    assert (result.removals_announced, result.removals_unpublished) == (1, 0)
    delisted = db.scalars(select(Event).where(Event.event_type == DELISTED_EVENT_TYPE)).one()
    control = db.scalars(select(Event).where(Event.event_type == "status_change")).one()
    q2 = db.get(Proposal, delisted.subject_id)
    assert q2 is not None and q2.sponsor_org_id is not None
    sponsor = db.get(Organization, q2.sponsor_org_id)
    assert sponsor is not None

    searches = {
        # Following one record: every event on it.
        "follow_record": _search(pro_user, pro, "follow_record", "event", {"subject_id": q2.public_id}),
        # Following a company: its proposals.
        "follow_sponsor": _search(
            pro_user, pro, "follow_sponsor", "proposal", {"sponsor_id": sponsor.public_id}
        ),
        # A saved search over the register's records.
        "saved_search": _search(
            pro_user, pro, "saved_search", "proposal", {"jurisdiction": "US-TX", "technology": "bess_li_ion"}
        ),
        "every_event": _search(pro_user, pro, "every_event", "event", {}),
    }
    for search in searches.values():
        # Each alert cycle after day 1 has already run, so day 2's events are the new ones.
        search.watermark_seq = day1_head
    db.add_all(searches.values())
    db.commit()
    return {
        "delisted": delisted,
        "control": control,
        "q2": q2,
        "searches": searches,
        "pro": pro,
        "pro_headers": {"Authorization": f"Bearer {pro_secret}"},
        "endpoint": endpoint,
    }


def _evt(ev: Event) -> str:
    return public_id("evt", ev.id)


def test_it_is_written_public_with_the_owner_wording(db: Session, world: dict[str, Any]) -> None:
    delisted = world["delisted"]
    assert delisted.event_type not in NON_PUBLIC_EVENT_TYPES and delisted.event_type in NON_SOCIAL_EVENT_TYPES
    assert delisted.published_at is not None and delisted.public_at is not None
    assert delisted.reason == SENTENCE
    assert delisted.after == {"source_id": SRC, "register_name": "ERCOT", "reason": "not stated"}
    q2 = db.get(Proposal, delisted.subject_id)
    assert q2 is not None and q2.lifecycle_state == "filed", "a departure does not move the lifecycle"
    assert not db.scalars(select(Event).where(Event.event_type == "withdrawn")).all()


def test_the_event_list_detail_history_and_feeds_serve_it(client: Any, world: dict[str, Any]) -> None:
    delisted, control, q2 = world["delisted"], world["control"], world["q2"]
    for headers in ({}, world["pro_headers"]):
        tier = "pro" if headers else "public"
        listing = client.get("/v1/events", headers=headers)
        assert listing.status_code == 200, listing.text
        ids = [e["id"] for e in listing.json()["data"]]
        assert _evt(control) in ids and _evt(delisted) in ids, tier

        by_type = client.get("/v1/events", params={"event_type": DELISTED_EVENT_TYPE}, headers=headers)
        assert [e["id"] for e in by_type.json()["data"]] == [_evt(delisted)], tier

        detail = client.get(f"/v1/events/{_evt(delisted)}", headers=headers)
        assert detail.status_code == 200, tier
        body = detail.json()["data"]
        assert body["event_type"] == DELISTED_EVENT_TYPE and body["reason"] == SENTENCE, tier
        assert body["headline"] == f"Project Q2: {SENTENCE}", tier
        assert body["after"]["register_name"] == "ERCOT" and body["published_at"], tier
        assert body["provenance"]["source_id"] == SRC, tier

        history = client.get(f"/v1/proposals/{q2.public_id}/events", headers=headers)
        assert history.status_code == 200, history.text
        assert sorted(e["event_type"] for e in history.json()["data"]) == ["created", "delisted"], tier

    for fmt in ("json", "rss"):
        feed = client.get("/feeds/events.json" if fmt == "json" else "/feeds/events.rss")
        assert feed.status_code == 200
        text = html.unescape(feed.text)
        assert _evt(control) in text and _evt(delisted) in text, fmt
        assert f"Project Q2: {SENTENCE}" in text, fmt
        assert "withdrawn" not in text, fmt
    filtered = client.get("/feeds/events.json", params={"event_type": DELISTED_EVENT_TYPE})
    items = filtered.json()["items"]
    assert len(items) == 1 and items[0]["title"] == f"Project Q2: {SENTENCE}"

    vocabulary = client.get("/v1/meta/vocabularies").json()["data"]["event_type"]
    assert DELISTED_EVENT_TYPE in [v["value"] for v in vocabulary]


def test_alerts_fire_for_a_followed_record_a_followed_sponsor_and_a_saved_search(
    db: Session, world: dict[str, Any]
) -> None:
    delisted, control, pro = world["delisted"], world["control"], world["pro"]
    for name in ("follow_record", "follow_sponsor", "saved_search", "every_event"):
        search = db.get(SavedSearch, world["searches"][name].id)
        assert search is not None
        matched = evaluate_saved_search(db, search, pro)
        on_delisted = [m for m in matched if m.event_seq == delisted.seq]
        assert len(on_delisted) == 1, name
        assert on_delisted[0].name == "Project Q2" and on_delisted[0].change == SENTENCE, name
        assert not any("withdrawn" in (m.change or "") for m in matched), name
        body = render_digest_body(search, matched, unsubscribe_token="ut_test")
        assert f"- Project Q2: {SENTENCE}" in body, name
        assert "withdrawn" not in body, name
        if name == "every_event":
            assert control.seq in [m.event_seq for m in matched]

    feed_items = matching_items_for_feed(db, world["searches"]["every_event"], pro)
    by_type = {i["platform_ext"]["event_type"]: i for i in feed_items}
    assert by_type[DELISTED_EVENT_TYPE]["title"] == f"proposal: {SENTENCE}"
    assert by_type["status_change"]["title"] == "proposal: status filed → studied"


class _Transport:
    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> Any:
        self.bodies.append(content)
        return type("Response", (), {"status_code": 200})()


def test_webhooks_enqueue_and_deliver_it(db: Session, world: dict[str, Any]) -> None:
    delisted, control = world["delisted"], world["control"]
    endpoint = db.get(WebhookEndpoint, world["endpoint"].id)
    assert endpoint is not None
    assert webhooks.webhook_type_for_event(delisted) == "event.published"
    assert webhooks.event_visible_for_endpoint(db, endpoint, delisted)
    assert len(webhooks.enqueue_deliveries_for_event(db, delisted)) == 1
    db.rollback()

    endpoint = db.get(WebhookEndpoint, world["endpoint"].id)
    assert endpoint is not None
    endpoint.watermark_seq = 0
    db.flush()
    webhooks.enqueue_new_deliveries(db)
    by_seq = {d.event_seq: d for d in db.scalars(select(WebhookDelivery))}
    assert control.seq in by_seq and delisted.seq in by_seq

    transport = _Transport()
    delivered = webhooks.attempt_delivery(db, by_seq[delisted.seq], transport=transport, secret="s3cret")
    assert delivered.status == "delivered"
    payload = json.loads(transport.bodies[0])
    assert payload["type"] == "event.published"
    assert payload["data"]["event"]["event_type"] == DELISTED_EVENT_TYPE
    assert payload["data"]["event"]["after"] == {
        "source_id": SRC,
        "register_name": "ERCOT",
        "reason": "not stated",
    }
    assert payload["data"]["subject"]["name_canonical"] == "Project Q2"


def test_no_social_draft_is_made_from_it(
    db: Session,
    db_sessionmaker: sessionmaker[Session],
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delisted = world["delisted"]
    assert DELISTED_EVENT_TYPE not in db_events._PROPOSAL_EVENT_TYPE_MAP
    assert DELISTED_EVENT_TYPE not in db_events._OPPORTUNITY_EVENT_TYPE_MAP
    assert social_event_from_db(db, delisted) is None

    # The refusal is by name, not only the maps' default-deny: a later map entry cannot draft it.
    monkeypatch.setitem(db_events._PROPOSAL_EVENT_TYPE_MAP, DELISTED_EVENT_TYPE, "proposal.withdrawn")
    assert social_event_from_db(db, delisted) is None

    report = draft_posts_tick(db_sessionmaker)
    assert report.errors == ()
    assert db.scalars(select(Post).where(Post.event_id == delisted.id)).all() == []

    assert signal_from_event(delisted, subject_name="Project Q2", subject_url="https://example.org/p") is None
