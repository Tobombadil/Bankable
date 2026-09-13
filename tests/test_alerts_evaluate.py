"""The alert evaluation job: saved-search matching against new events, digest grouping, and
delivery through the (dry-run) `EmailPort` (task brief item 3 and its named tests;
docs/23-api-spec-outline.md §3.2)."""

from __future__ import annotations

import datetime as dt

from services.alerts.evaluate import evaluate_saved_search, run_alert_cycle
from services.alerts.matching import (
    event_matches_query,
    opportunity_matches_query,
    proposal_matches_query,
)
from services.api.auth import ResendEmailAdapter
from services.api.conftest import (
    make_event,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Alert, SavedSearch
from tests.conftest import make_account, make_user

UTC = dt.UTC


def _make_saved_search(db, account, user, *, entity="proposal", query=None, channels=None) -> SavedSearch:
    from services.ids import public_id

    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="test search",
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


# ------------------------------------------------------------------------------------- matching
def test_proposal_matches_query_by_kind_jurisdiction_and_capacity(db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "storage"
    prop.jurisdiction = "US-TX"
    prop.capacity_mw = 120.0

    assert proposal_matches_query(prop, {"kind": "storage"}) is True
    assert proposal_matches_query(prop, {"kind": "generation"}) is False
    assert proposal_matches_query(prop, {"jurisdiction": "US-TX,US-CA"}) is True
    assert proposal_matches_query(prop, {"capacity_mw[gte]": 50}) is True
    assert proposal_matches_query(prop, {"capacity_mw[gte]": 500}) is False
    assert proposal_matches_query(prop, {}) is True  # no filters, matches everything


def test_opportunity_matches_query_by_technologies_and_due_date(db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    opp = make_visible_opportunity(db, src, public_id_suffix="1")
    opp.technologies = ["solar_pv", "bess"]

    assert opportunity_matches_query(opp, {"technologies": "bess"}) is True
    assert opportunity_matches_query(opp, {"technologies": "wind"}) is False
    assert opportunity_matches_query(opp, {"status": "open"}) is True
    assert opportunity_matches_query(opp, {"status": "closed"}) is False


def test_event_matches_query_by_type(db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src, event_type="status_change")

    assert event_matches_query(event, {"event_type": "status_change"}) is True
    assert event_matches_query(event, {"event_type": "merged"}) is False


# ---------------------------------------------------------------------- evaluate_saved_search
def test_evaluate_saved_search_advances_watermark_even_without_a_match(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "generation"
    event = make_event(db, prop, src)
    search = _make_saved_search(db, account, user, query={"kind": "storage"})
    db.commit()

    matched = evaluate_saved_search(db, search, account)
    assert matched == []
    assert search.watermark_seq == event.seq, "a non-matching event still advances the watermark"


def test_evaluate_saved_search_is_exactly_once(db):
    """A second pass with no new events since the watermark returns nothing — the same event is
    never re-matched (docs/21 §3.15 `watermark_seq`)."""
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "storage"
    make_event(db, prop, src)
    search = _make_saved_search(db, account, user, query={"kind": "storage"})
    db.commit()

    first = evaluate_saved_search(db, search, account)
    assert len(first) == 1
    second = evaluate_saved_search(db, search, account)
    assert second == []


# ---------------------------------------------------------------------------- digest grouping
def test_run_alert_cycle_groups_multiple_matches_into_one_digest_alert(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)

    p1 = make_visible_proposal(db, src, public_id_suffix="1")
    p1.kind = "storage"
    p2 = make_visible_proposal(db, src, public_id_suffix="2")
    p2.kind = "storage"
    p3 = make_visible_proposal(db, src, public_id_suffix="3")
    p3.kind = "generation"  # should not match

    make_event(db, p1, src)
    make_event(db, p2, src)
    make_event(db, p3, src)

    search = _make_saved_search(db, account, user, query={"kind": "storage"})
    db.commit()

    email = ResendEmailAdapter()
    created = run_alert_cycle(db, email_port=email)

    assert len(created) == 1, "one digest alert, not one alert per matched record"
    alert = created[0]
    assert alert.channel == "email"
    assert alert.status == "sent"
    assert len(alert.event_seqs) == 2, "both storage-proposal events are in the one digest"
    assert search.last_match_count == 2

    assert len(email.sent) == 1
    body = email.sent[0].body
    assert p1.name_canonical in body
    assert p2.name_canonical in body
    assert p3.name_canonical not in body
    # Attribution and the source link are in every alert body (task brief item 3).
    assert src.name in body or (src.attribution_text or "") in body
    assert f"/proposals/{p1.slug}" in body


def test_run_alert_cycle_is_a_no_op_when_nothing_matches(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "generation"
    make_event(db, prop, src)
    _make_saved_search(db, account, user, query={"kind": "storage"})
    db.commit()

    email = ResendEmailAdapter()
    created = run_alert_cycle(db, email_port=email)
    assert created == []
    assert email.sent == []
    assert db.query(Alert).count() == 0


def test_run_alert_cycle_skips_paused_searches(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "storage"
    make_event(db, prop, src)
    search = _make_saved_search(db, account, user, query={"kind": "storage"})
    search.status = "paused"
    db.commit()

    email = ResendEmailAdapter()
    created = run_alert_cycle(db, email_port=email)
    assert created == []


def test_rss_channel_never_produces_a_stored_alert_row(db):
    """`rss` is served live from the current query (docs/23 §9.2), never written as an `alert`
    row — only `email` produces one here."""
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "storage"
    make_event(db, prop, src)
    _make_saved_search(db, account, user, query={"kind": "storage"}, channels=["rss"])
    db.commit()

    email = ResendEmailAdapter()
    created = run_alert_cycle(db, email_port=email)
    assert created == []
    assert db.query(Alert).count() == 0


def test_event_entity_saved_search_matches_on_event_type(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    make_event(db, prop, src, event_type="status_change")
    _search = _make_saved_search(db, account, user, entity="event", query={"event_type": "status_change"})
    db.commit()

    email = ResendEmailAdapter()
    created = run_alert_cycle(db, email_port=email)
    assert len(created) == 1
    assert len(created[0].event_seqs) == 1
