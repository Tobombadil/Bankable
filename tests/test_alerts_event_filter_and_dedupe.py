"""`services/alerts/evaluate.py::evaluate_saved_search`: the event-search filter and the
one-line-per-record rule (audit 2026-10-07 QA-9).

The old tests reached neither line. One is the `continue` that drops an event a saved event search
does not match. The other is the `seen_subjects` check that lists a record once, however many new
events it has. Without the filter, an event search alerts on every visible event. Without the check,
a record with three new events appears three times in one digest. Each test below fails if its
line is removed.
"""

from __future__ import annotations

from services.alerts.evaluate import evaluate_saved_search, run_alert_cycle
from services.api.auth import ResendEmailAdapter
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import SavedSearch
from services.ids import public_id
from tests.conftest import make_account, make_user


def _search(db, account, user, *, entity: str, query: dict) -> SavedSearch:
    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="qa-9 search",
        entity=entity,
        query=query,
        query_hash="x",
        channels=["email"],
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    return search


def test_an_event_search_alerts_only_on_the_events_its_query_matches(db) -> None:
    account = make_account(db)
    user = make_user(db, account)
    src = make_public_source(db, make_open_licence(db))
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    other = make_visible_proposal(db, src, public_id_suffix="2")
    wanted = make_event(db, prop, src, event_type="status_change")
    unwanted = make_event(db, other, src, event_type="capacity_change")
    search = _search(db, account, user, entity="event", query={"event_type": "status_change"})
    db.commit()

    matched = evaluate_saved_search(db, search, account)

    assert [m.event_seq for m in matched] == [wanted.seq]
    assert all(m.kind == "event" for m in matched)
    assert other.name_canonical not in {m.name for m in matched}
    # The non-matching event is still consumed: the watermark passes it.
    assert search.watermark_seq == max(wanted.seq, unwanted.seq)


def test_an_event_search_with_no_match_returns_nothing_but_advances(db) -> None:
    account = make_account(db)
    user = make_user(db, account)
    src = make_public_source(db, make_open_licence(db))
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src, event_type="capacity_change")
    search = _search(db, account, user, entity="event", query={"event_type": "merged"})
    db.commit()

    assert evaluate_saved_search(db, search, account) == []
    assert search.watermark_seq == event.seq


def test_a_record_with_several_new_events_is_listed_once(db) -> None:
    account = make_account(db)
    user = make_user(db, account)
    src = make_public_source(db, make_open_licence(db))
    busy = make_visible_proposal(db, src, public_id_suffix="1")
    quiet = make_visible_proposal(db, src, public_id_suffix="2")
    first = make_event(db, busy, src, event_type="status_change")
    make_event(db, busy, src, event_type="capacity_change")
    make_event(db, busy, src, event_type="cod_change")
    single = make_event(db, quiet, src, event_type="status_change")
    search = _search(db, account, user, entity="proposal", query={"kind": "storage"})
    db.commit()

    matched = evaluate_saved_search(db, search, account)

    assert [m.name for m in matched] == [busy.name_canonical, quiet.name_canonical]
    assert [m.event_seq for m in matched] == [first.seq, single.seq], "each record's first new event"


def test_the_digest_body_names_a_busy_record_on_one_line(db) -> None:
    account = make_account(db)
    user = make_user(db, account)
    src = make_public_source(db, make_open_licence(db))
    busy = make_visible_proposal(db, src, public_id_suffix="1")
    make_event(db, busy, src, event_type="status_change")
    make_event(db, busy, src, event_type="capacity_change")
    _search(db, account, user, entity="proposal", query={"kind": "storage"})
    db.commit()

    email = ResendEmailAdapter()
    created = run_alert_cycle(db, email_port=email)

    assert len(created) == 1
    assert len(created[0].event_seqs) == 1
    body = email.sent[0].body
    assert body.count(f"- {busy.name_canonical}") == 1
    assert "1 new match(es)" in body
