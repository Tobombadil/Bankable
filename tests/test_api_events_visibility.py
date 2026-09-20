"""Events are visible only when their subject is (2026-09-18 audit, docs/50 §3.1 "the events feed
... bypass[es] the record-level visibility gate and leak[s] names, URLs and transitions of hidden
and restricted records"; docs/21 §8 item 2). An event on a hidden proposal, on a
restricted-licence proposal and on a not-yet-public proposal is absent from `GET /v1/events`,
`GET /v1/events/{id}` and `/feeds/events.{rss,json}` on the public tier; Pro sees the
not-yet-public one (its `published_at` has passed) and never the other two.
"""

from __future__ import annotations

import datetime as dt

from services.api.conftest import (
    make_event,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Licence, Source
from tests.conftest import make_account, make_api_key, make_user

UTC = dt.UTC


def _restricted_source(db) -> Source:
    lic = Licence(id="restricted-lic", name="Restricted", reuse_class="restricted", gate_flag=True)
    db.add(lic)
    db.flush()
    src = Source(
        id="us.test.restricted_source",
        name="Restricted Source",
        category="generation_queue",
        url="https://example.org/restricted",
        access="html",
        cadence="weekly",
        licence_id=lic.id,
        publish_state="public",
    )
    db.add(src)
    db.flush()
    return src


def _seed(db):
    """Four proposals, each with one event whose *own* licence/source/timing clauses pass (open
    licence, public source, `public_at` in the past) so only the subject join can exclude it."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)

    shown = make_visible_proposal(db, src, public_id_suffix="1")
    shown.name_canonical = "Visible Project"
    ev_shown = make_event(db, shown, src)

    hidden = make_visible_proposal(db, src, public_id_suffix="2")
    hidden.name_canonical = "Hidden Project Name"
    hidden.publish_state = "unpublished"
    ev_hidden = make_event(db, hidden, src)

    restricted_src = _restricted_source(db)
    restricted = make_visible_proposal(db, restricted_src, public_id_suffix="3")
    restricted.name_canonical = "Restricted Project Name"
    restricted.min_reuse_class = "restricted"
    # The event itself is recorded under the open licence: the old per-event check passed it.
    ev_restricted = make_event(db, restricted, src)

    pending = make_visible_proposal(db, src, public_id_suffix="4", public_at=now + dt.timedelta(days=5))
    pending.name_canonical = "Pending Project Name"
    pending.published_at = now - dt.timedelta(minutes=1)
    ev_pending = make_event(db, pending, src)
    db.commit()
    return {
        "shown": (shown, ev_shown),
        "hidden": (hidden, ev_hidden),
        "restricted": (restricted, ev_restricted),
        "pending": (pending, ev_pending),
    }


def _event_id(ev) -> str:
    from services.ids import public_id

    return public_id("evt", ev.id)


def _pro_headers(db) -> dict[str, str]:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    return {"Authorization": f"Bearer {secret}"}


LEAK_NAMES = ("Hidden Project Name", "Restricted Project Name")


def test_public_events_list_and_feeds_carry_only_visible_subjects(client, db):
    seeded = _seed(db)
    shown, ev_shown = seeded["shown"]

    listing = client.get("/v1/events")
    assert listing.status_code == 200
    body = listing.json()
    assert [e["subject_id"] for e in body["data"]] == [shown.public_id]
    assert [e["id"] for e in body["data"]] == [_event_id(ev_shown)]
    text = listing.text
    for name in (*LEAK_NAMES, "Pending Project Name"):
        assert name not in text

    for fmt in ("json", "rss"):
        feed = client.get(f"/feeds/events.{fmt}")
        assert feed.status_code == 200
        assert "Visible Project" in feed.text
        for name in (*LEAK_NAMES, "Pending Project Name"):
            assert name not in feed.text, fmt
        for key in ("hidden", "restricted", "pending"):
            assert seeded[key][0].slug not in feed.text, (fmt, key)
    json_feed = client.get("/feeds/events.json").json()
    assert [it["id"] for it in json_feed["items"]] == [_event_id(ev_shown)]


def test_public_event_by_id_is_404_for_invisible_subjects(client, db):
    seeded = _seed(db)
    assert client.get(f"/v1/events/{_event_id(seeded['shown'][1])}").status_code == 200
    for key in ("hidden", "restricted", "pending"):
        resp = client.get(f"/v1/events/{_event_id(seeded[key][1])}")
        assert resp.status_code == 404, key
        for name in LEAK_NAMES:
            assert name not in resp.text


def test_subject_id_filter_cannot_probe_an_invisible_subject(client, db):
    seeded = _seed(db)
    for key in ("hidden", "restricted", "pending"):
        body = client.get("/v1/events", params={"subject_id": seeded[key][0].public_id}).json()
        assert body["data"] == [], key


def test_pro_sees_the_pending_subject_but_never_hidden_or_restricted(client, db):
    seeded = _seed(db)
    headers = _pro_headers(db)

    body = client.get("/v1/events", headers=headers).json()
    assert body["meta"]["tier"] == "pro"
    ids = sorted(e["subject_id"] for e in body["data"])
    assert ids == sorted([seeded["shown"][0].public_id, seeded["pending"][0].public_id])
    for name in LEAK_NAMES:
        assert name not in str(body)

    assert client.get(f"/v1/events/{_event_id(seeded['pending'][1])}", headers=headers).status_code == 200
    assert client.get(f"/v1/events/{_event_id(seeded['hidden'][1])}", headers=headers).status_code == 404
    assert client.get(f"/v1/events/{_event_id(seeded['restricted'][1])}", headers=headers).status_code == 404


def test_event_on_a_non_record_subject_is_never_public(client, db):
    """`user` audit events (`services/api/admin_people.py`) and any future subject type fail
    closed rather than rendering as "Unknown"."""
    from services.db.models import Event

    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)
    ev = Event(
        subject_type="user",
        subject_id=make_visible_proposal(db, src).id,  # any uuid; the type is what matters
        event_type="personal_data_redacted",
        observed_at=now - dt.timedelta(days=2),
        source_id=src.id,
        source_url=src.url,
        retrieved_at=now - dt.timedelta(days=2),
        licence_id=src.licence_id,
        before={"email": "x"},
        after={"email": None},
        changed_keys=["email"],
        public_at=now - dt.timedelta(days=1),
        published_at=now - dt.timedelta(days=1),
        actor_type="user",
        idempotency_key="user-event-1",
    )
    db.add(ev)
    db.commit()

    body = client.get("/v1/events").json()
    assert all(e["subject_type"] != "user" for e in body["data"])
    assert client.get(f"/v1/events/{_event_id(ev)}").status_code == 404


# --------------------------------------------------------------------------- opportunity subjects
# `event_visibility_filter`'s subject clause is an `or_` of two arms; every test above reaches it
# through the `proposal` arm only. The `opportunity` arm had no test at all (the CI coverage step's
# note called this out as "line 164 ... which the suite reaches only through the proposal arm";
# after the module was rewritten the arm is no longer a statement of its own, so coverage stopped
# flagging it while the gap stayed open). These three tests close it in both directions: an event
# on a visible opportunity is *served*, and events on hidden, restricted and not-yet-public
# opportunities are not.


def _opportunity_event(db, opp, source, *, suffix: str = "1", event_type: str = "status_change"):
    from services.db.models import Event

    now = dt.datetime.now(UTC)
    ev = Event(
        subject_type="opportunity",
        subject_id=opp.id,
        event_type=event_type,
        observed_at=now - dt.timedelta(days=2),
        source_id=source.id,
        source_url=source.url,
        retrieved_at=now - dt.timedelta(days=2),
        licence_id=source.licence_id,
        before={"status": "open"},
        after={"status": "closed"},
        changed_keys=["status"],
        public_at=now - dt.timedelta(days=1),
        published_at=now - dt.timedelta(days=1),
        idempotency_key=f"opp-event-{suffix}",
    )
    db.add(ev)
    db.flush()
    return ev


def _seed_opportunities(db):
    """The opportunity twin of `_seed`: four opportunities, each with one event whose *own*
    licence/source/timing clauses pass, so only the subject join can exclude it."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)

    shown = make_visible_opportunity(db, src, public_id_suffix="1")
    shown.title = "Visible RFP"
    ev_shown = _opportunity_event(db, shown, src, suffix="1")

    hidden = make_visible_opportunity(db, src, public_id_suffix="2")
    hidden.title = "Hidden RFP Title"
    hidden.publish_state = "unpublished"
    ev_hidden = _opportunity_event(db, hidden, src, suffix="2")

    restricted_src = _restricted_source(db)
    restricted = make_visible_opportunity(db, restricted_src, public_id_suffix="3")
    restricted.title = "Restricted RFP Title"
    restricted.min_reuse_class = "restricted"
    # Recorded under the open licence, like the proposal seed: the per-event clauses pass.
    ev_restricted = _opportunity_event(db, restricted, src, suffix="3")

    pending = make_visible_opportunity(db, src, public_id_suffix="4")
    pending.title = "Pending RFP Title"
    pending.public_at = now + dt.timedelta(days=5)
    pending.published_at = now - dt.timedelta(minutes=1)
    ev_pending = _opportunity_event(db, pending, src, suffix="4")
    db.commit()
    return {
        "shown": (shown, ev_shown),
        "hidden": (hidden, ev_hidden),
        "restricted": (restricted, ev_restricted),
        "pending": (pending, ev_pending),
    }


OPPORTUNITY_LEAK_NAMES = ("Hidden RFP Title", "Restricted RFP Title", "Pending RFP Title")


def test_public_events_list_and_feeds_carry_an_event_on_a_visible_opportunity(client, db):
    seeded = _seed_opportunities(db)
    shown, ev_shown = seeded["shown"]

    body = client.get("/v1/events").json()
    assert [e["id"] for e in body["data"]] == [_event_id(ev_shown)]
    assert [e["subject_type"] for e in body["data"]] == ["opportunity"]
    assert [e["subject_id"] for e in body["data"]] == [shown.public_id]

    filtered = client.get("/v1/events", params={"subject_type": "opportunity"}).json()
    assert [e["id"] for e in filtered["data"]] == [_event_id(ev_shown)]

    for fmt in ("json", "rss"):
        feed = client.get(f"/feeds/events.{fmt}")
        assert feed.status_code == 200
        assert "Visible RFP" in feed.text, fmt
        for name in OPPORTUNITY_LEAK_NAMES:
            assert name not in feed.text, (fmt, name)
        for key in ("hidden", "restricted", "pending"):
            assert seeded[key][0].slug not in feed.text, (fmt, key)


def test_public_event_by_id_is_404_for_invisible_opportunity_subjects(client, db):
    seeded = _seed_opportunities(db)
    assert client.get(f"/v1/events/{_event_id(seeded['shown'][1])}").status_code == 200
    for key in ("hidden", "restricted", "pending"):
        resp = client.get(f"/v1/events/{_event_id(seeded[key][1])}")
        assert resp.status_code == 404, key
        for name in OPPORTUNITY_LEAK_NAMES:
            assert name not in resp.text
        assert (
            client.get("/v1/events", params={"subject_id": seeded[key][0].public_id}).json()["data"] == []
        ), key


def test_pro_sees_the_pending_opportunity_subject_but_never_hidden_or_restricted(client, db):
    seeded = _seed_opportunities(db)
    headers = _pro_headers(db)

    body = client.get("/v1/events", headers=headers).json()
    assert body["meta"]["tier"] == "pro"
    assert sorted(e["subject_id"] for e in body["data"]) == sorted(
        [seeded["shown"][0].public_id, seeded["pending"][0].public_id]
    )
    for name in ("Hidden RFP Title", "Restricted RFP Title"):
        assert name not in str(body)

    assert client.get(f"/v1/events/{_event_id(seeded['pending'][1])}", headers=headers).status_code == 200
    assert client.get(f"/v1/events/{_event_id(seeded['hidden'][1])}", headers=headers).status_code == 404
    assert client.get(f"/v1/events/{_event_id(seeded['restricted'][1])}", headers=headers).status_code == 404
