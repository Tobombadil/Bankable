"""Item 1 of the 2026-09-18 blockers sprint (docs/50-audit-2026-09-18.md §3.1: "the events feed
and Pro digest emails bypass the record-level visibility gate and leak names, URLs and transitions
of hidden and restricted records").

Each case builds an event that passes the *event-level* predicate on its own (its source public,
its licence open, its `published_at` reached) while the *record* it describes does not — the exact
gap `services/alerts/visibility.py` closes — and proves the record never reaches a digest body, an
`Alert.event_seqs`, the private feed, a webhook delivery or a replay.
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.alerts.evaluate import run_alert_cycle
from services.alerts.feed import matching_items_for_feed
from services.alerts.mail import ResendAlertMailer
from services.alerts.webhooks import (
    attempt_delivery,
    enqueue_deliveries_for_event,
    event_visible_for_endpoint,
    replay_from_seq,
)
from services.api.conftest import (
    make_event,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import Alert, Licence, SavedSearch, WebhookEndpoint
from services.ids import public_id
from tests.conftest import make_account, make_user

UTC = dt.UTC


@pytest.fixture(autouse=True)
def _fake_identity(monkeypatch):
    monkeypatch.setenv("SENDER_LEGAL_NAME", "Test Sender Ltd (not a real entity)")
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "1 Test Street, Testville (fake)")
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-not-a-secret")


def _saved_search(db, account, user, *, entity="proposal", query=None, channels=None) -> SavedSearch:
    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="everything",
        entity=entity,
        query=query or {},
        query_hash="x",
        channels=channels or ["email"],
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    return search


def _endpoint(db, account, user) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        public_id="",
        account_id=account.id,
        created_by_user_id=user.id,
        url="https://example.com/hook",
        types=["event.published"],
        entity="event",
        query={},
        secret="whsec_test_not_a_secret",
        status="active",
    )
    db.add(endpoint)
    db.flush()
    endpoint.public_id = public_id("wh", endpoint.id)
    db.flush()
    return endpoint


def _restricted_licence(db) -> Licence:
    lic = Licence(
        id="restricted-lic",
        name="Restricted Licence",
        reuse_class="restricted",
        allows_derived_publication=True,
        allows_raw_publication=False,
        allows_api_redistribution=False,
        allows_bulk_export=False,
        allows_commercial_use=False,
        gate_flag=True,
        evidence_url="https://example.org/restricted",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


class _Case:
    """One visible control record plus one record the digest must not carry."""

    def __init__(self, db, *, entitlement: str = "pro") -> None:
        self.account = make_account(db, entitlement=entitlement)
        self.user = make_user(db, self.account)
        self.lic = make_open_licence(db)
        self.src = make_public_source(db, self.lic)
        self.visible = make_visible_proposal(db, self.src, public_id_suffix="1")
        self.visible_event = make_event(db, self.visible, self.src)


def _hidden_record(db, case: _Case):
    hidden = make_visible_proposal(db, case.src, public_id_suffix="2")
    hidden.name_canonical = "Hidden Secret Project"
    hidden.publish_state = "unpublished"
    event = make_event(db, hidden, case.src)  # the event itself passes the event-level predicate
    db.flush()
    return hidden, event


def _restricted_record(db, case: _Case):
    restricted = make_visible_proposal(db, case.src, public_id_suffix="3")
    restricted.name_canonical = "Restricted Licence Project"
    restricted.min_reuse_class = "restricted"
    _restricted_licence(db)
    event = make_event(db, restricted, case.src)
    db.flush()
    return restricted, event


def _unpublished_event(db, case: _Case):
    """A visible record whose *event* has not reached `published_at` yet (nor `public_at`)."""
    prop = make_visible_proposal(db, case.src, public_id_suffix="4")
    prop.name_canonical = "Future Transition Project"
    future = dt.datetime.now(UTC) + dt.timedelta(days=3)
    event = make_event(db, prop, case.src, public_at=future)
    event.published_at = future
    db.flush()
    return prop, event


# ------------------------------------------------------------------------------------ digest
@pytest.mark.parametrize("build", [_hidden_record, _restricted_record, _unpublished_event])
def test_digest_never_carries_a_record_the_predicate_hides(db, build):
    case = _Case(db)
    leaked, leaked_event = build(db, case)
    _saved_search(db, case.account, case.user)
    db.commit()

    mailer = ResendAlertMailer(api_key="")
    created = run_alert_cycle(db, email_port=mailer)

    assert len(created) == 1
    alert = created[0]
    assert alert.status == "sent"
    assert alert.event_seqs == [case.visible_event.seq]
    assert leaked_event.seq not in alert.event_seqs
    body = mailer.sent[0].body
    assert case.visible.name_canonical in body
    assert leaked.name_canonical not in body
    assert leaked.slug not in body
    assert db.query(Alert).count() == 1


def test_hidden_record_is_gated_for_the_api_tier_too(db):
    """`licence_permits` and `publish_state` are unconditional on tier (services/api/visibility.py
    module docstring): an `api` account does not see the hidden record either."""
    case = _Case(db, entitlement="api")
    leaked, _ = _hidden_record(db, case)
    _saved_search(db, case.account, case.user)
    db.commit()

    mailer = ResendAlertMailer(api_key="")
    created = run_alert_cycle(db, email_port=mailer)
    assert leaked.name_canonical not in mailer.sent[0].body
    assert len(created[0].event_seqs) == 1


def test_event_entity_search_skips_events_on_hidden_records(db):
    """The `entity = event` path names only `subject_type: event_type`, but its `event_seqs` and
    count would still betray that a hidden record changed; it is gated the same way."""
    case = _Case(db)
    _, leaked_event = _hidden_record(db, case)
    _saved_search(db, case.account, case.user, entity="event", query={"event_type": "status_change"})
    db.commit()

    created = run_alert_cycle(db, email_port=ResendAlertMailer(api_key=""))
    assert created[0].event_seqs == [case.visible_event.seq]
    assert leaked_event.seq not in created[0].event_seqs


# -------------------------------------------------------------------------------------- feed
def test_private_event_feed_skips_events_on_hidden_and_restricted_records(db):
    case = _Case(db)
    _, hidden_event = _hidden_record(db, case)
    _, restricted_event = _restricted_record(db, case)
    search = _saved_search(db, case.account, case.user, entity="event", channels=["rss"])
    db.commit()

    items = matching_items_for_feed(db, search, case.account)
    guids = {item["guid"] for item in items}
    assert str(case.visible_event.id) in guids
    assert str(hidden_event.id) not in guids
    assert str(restricted_event.id) not in guids


# ----------------------------------------------------------------------------------- webhooks
@pytest.mark.parametrize("build", [_hidden_record, _restricted_record, _unpublished_event])
def test_webhook_enqueue_and_replay_skip_records_the_predicate_hides(db, build):
    case = _Case(db)
    _leaked, leaked_event = build(db, case)
    endpoint = _endpoint(db, case.account, case.user)
    db.commit()

    assert event_visible_for_endpoint(db, endpoint, case.visible_event) is True
    assert event_visible_for_endpoint(db, endpoint, leaked_event) is False
    assert enqueue_deliveries_for_event(db, leaked_event) == []
    assert len(enqueue_deliveries_for_event(db, case.visible_event)) == 1

    replayed = replay_from_seq(db, endpoint, since_seq=0)
    assert [d.event_seq for d in replayed] == [case.visible_event.seq]


def test_webhook_delivery_refuses_a_record_hidden_after_enqueue(db):
    """Enqueued while visible, hidden before the worker posts it: the payload (which would carry
    the record's name and transition) is never sent, and the endpoint's failure streak is left
    alone — the customer's endpoint did nothing wrong."""
    case = _Case(db)
    endpoint = _endpoint(db, case.account, case.user)
    db.commit()
    (delivery,) = enqueue_deliveries_for_event(db, case.visible_event)
    case.visible.publish_state = "unpublished"
    db.flush()

    posted: list[dict] = []

    class _Transport:
        def post(self, url, *, content, headers):
            posted.append({"url": url, "content": content})

            class _R:
                status_code = 200

            return _R()

    result = attempt_delivery(db, delivery, transport=_Transport(), secret="whsec_test_not_a_secret")
    assert posted == []
    assert result.status == "failed"
    assert result.error_class == "SubjectNotVisible"
    assert endpoint.consecutive_failures == 0
