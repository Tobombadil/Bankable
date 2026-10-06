"""What an alert email says (owner decision 2026-09-30; content audit F7) and when a digest is due
(`services/alerts/evaluate.py`): every line names its record, says what changed and links to the
record's page; daily and weekly digests are evaluated once per period."""

from __future__ import annotations

import datetime as dt

from services.alerts.evaluate import (
    DIGEST_MAX_ITEMS,
    MatchedItem,
    describe_change,
    describe_subject,
    evaluate_saved_search,
    is_due,
    render_digest_body,
    results_url,
    run_alert_cycle,
)
from services.alerts.mail import ResendAlertMailer
from services.api.common import WEB_HOST
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Event, SavedSearch
from services.ids import public_id
from tests.conftest import make_account, make_user

UTC = dt.UTC


def _search(db, account, user, *, entity="proposal", query=None, mode="daily", name="Watch"):
    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name=name,
        entity=entity,
        query=query or {},
        query_hash="x",
        delivery_mode=mode,
        channels=["email"],
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    return search


def _world(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    return account, user, src, prop


def _event(**fields):
    base = {"event_type": "status_change", "before": None, "after": None, "changed_keys": []}
    base.update(fields)
    return Event(**base)


# --------------------------------------------------------------------------------- describe_change
def test_describe_change_names_the_transition() -> None:
    ev = _event(
        before={"lifecycle_state": "filed"},
        after={"lifecycle_state": "under_construction"},
        changed_keys=["lifecycle_state"],
    )
    assert describe_change(ev) == "status filed → under construction"


def test_describe_change_formats_capacity_and_dates() -> None:
    cap = _event(
        event_type="capacity_changed",
        before={"capacity_mw": 100.0},
        after={"capacity_mw": 150.5},
        changed_keys=["capacity_mw"],
    )
    assert describe_change(cap) == "capacity 100 MW → 150.5 MW"
    cod = _event(
        event_type="field_changed",
        before={"proposed_cod": "2027-06-01 00:00:00"},
        after={"proposed_cod": "2028-01-15T00:00:00"},
        changed_keys=["proposed_cod"],
    )
    assert describe_change(cod) == "target online date 2027-06-01 → 2028-01-15"


def test_describe_change_new_and_withdrawn() -> None:
    assert describe_change(_event(event_type="created", after={"lifecycle_state": "filed"})) == "new record"
    gone = _event(
        event_type="withdrawn", before={"lifecycle_state": "filed"}, changed_keys=["lifecycle_state"]
    )
    assert describe_change(gone) == "status filed no longer reported"
    assert describe_change(_event(event_type="withdrawn")) == "withdrawn"


def test_describe_subject_gives_size_technology_and_place(db) -> None:
    _account, _user, _src, prop = _world(db)
    assert describe_subject(prop) == "101 MW bess li ion, US-TX"
    prop.capacity_mw = None
    prop.technology = "load"
    assert describe_subject(prop) == "large load, US-TX"


# ------------------------------------------------------------------------------ what a digest says
def test_an_event_alert_names_the_record_says_what_changed_and_links_to_it(db) -> None:
    account, user, src, prop = _world(db)
    make_event(db, prop, src)
    search = _search(db, account, user, entity="event")
    db.commit()

    items = evaluate_saved_search(db, search, account)
    assert len(items) == 1
    item = items[0]
    assert item.name == prop.name_canonical
    assert item.url == f"{WEB_HOST}/proposals/{prop.slug}"
    assert item.change == "status announced → filed"
    assert item.context == "101 MW bess li ion, US-TX"

    body = render_digest_body(search, items, unsubscribe_token="ut_x")
    assert f"- {prop.name_canonical}: status announced → filed (101 MW bess li ion, US-TX)" in body
    assert f"  {WEB_HOST}/proposals/{prop.slug} (source: " in body
    assert "proposal: status_change" not in body
    assert f"— {WEB_HOST} " not in body, "no bare homepage link stands in for the record"


def test_a_proposal_alert_also_says_what_changed(db) -> None:
    account, user, src, prop = _world(db)
    make_event(db, prop, src)
    _search(db, account, user)
    db.commit()
    mailer = ResendAlertMailer(api_key="")
    run_alert_cycle(db, email_port=mailer)
    body = mailer.sent[0].body
    assert f"- {prop.name_canonical}: status announced → filed (101 MW bess li ion, US-TX)" in body
    assert f"{WEB_HOST}/proposals/{prop.slug}" in body
    assert f"Manage your alerts: {WEB_HOST}/alerts" in body


def test_a_long_digest_lists_the_first_items_and_links_the_rest(db) -> None:
    account, user, _src, _prop = _world(db)
    search = _search(db, account, user, query={"kind": "storage", "jurisdiction": "US-TX"})
    items = [
        MatchedItem(
            kind="proposal",
            name=f"P{i}",
            url=f"{WEB_HOST}/proposals/p{i}",
            source_name="S",
            attribution_text=None,
            event_seq=i,
            change="new record",
        )
        for i in range(DIGEST_MAX_ITEMS + 7)
    ]
    body = render_digest_body(search, items, unsubscribe_token="ut_x")
    assert f"- P{DIGEST_MAX_ITEMS - 1}: new record" in body
    assert f"- P{DIGEST_MAX_ITEMS}: " not in body
    assert f"- and 7 more: {WEB_HOST}/proposals?kind=storage&jurisdiction=US-TX" in body


def test_results_url_uses_the_list_page_for_the_entity(db) -> None:
    account, user, _src, _prop = _world(db)
    opp = _search(db, account, user, entity="opportunity", query={"technologies": "solar,wind"}, name="o")
    assert results_url(opp) == f"{WEB_HOST}/opportunities?technologies=solar%2Cwind"


# ----------------------------------------------------------------------------------------- cadence
def test_is_due_honours_daily_and_weekly_with_one_tick_of_slack(db) -> None:
    account, user, _src, _prop = _world(db)
    now = dt.datetime(2026, 9, 30, 6, 0, tzinfo=UTC)
    daily = _search(db, account, user, name="d")
    assert is_due(daily, now), "never run: due"
    daily.last_run_at = now - dt.timedelta(hours=2)
    assert not is_due(daily, now)
    daily.last_run_at = now - dt.timedelta(hours=23, minutes=50)
    assert is_due(daily, now), "a tick a few minutes early does not slip a whole day"
    weekly = _search(db, account, user, mode="weekly", name="w")
    weekly.last_run_at = now - dt.timedelta(days=6)
    assert not is_due(weekly, now)
    weekly.last_run_at = now - dt.timedelta(days=7)
    assert is_due(weekly, now)
    immediate = _search(db, account, user, mode="immediate", name="i")
    immediate.last_run_at = now - dt.timedelta(minutes=15)
    assert is_due(immediate, now)


def test_a_daily_digest_is_not_sent_twice_in_one_day(db) -> None:
    account, user, src, prop = _world(db)
    make_event(db, prop, src)
    search = _search(db, account, user)
    db.commit()
    mailer = ResendAlertMailer(api_key="")
    first_at = dt.datetime.now(UTC)
    assert len(run_alert_cycle(db, email_port=mailer, now=first_at)) == 1
    db.commit()
    watermark = search.watermark_seq

    prop2 = make_visible_proposal(db, src, public_id_suffix="2")
    make_event(db, prop2, src, public_at=first_at - dt.timedelta(minutes=1))
    db.commit()
    later_same_day = first_at + dt.timedelta(hours=3)
    assert run_alert_cycle(db, email_port=mailer, now=later_same_day) == []
    assert search.watermark_seq == watermark, "the next digest still carries the new event"

    next_day = first_at + dt.timedelta(days=1)
    created = run_alert_cycle(db, email_port=mailer, now=next_day)
    assert len(created) == 1
    assert prop2.name_canonical in mailer.sent[-1].body
    assert len(mailer.sent) == 2
