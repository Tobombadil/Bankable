"""The events list's view filters (`services/api/change_rule.py`): `genuine`, `to_state`, `relevance`.

`genuine=true` is the daily metrics recorder's rule in SQL. It is held here to the recorder itself:
for every UTC day of a store holding one of each case the rule names (a source's first run, a later
scheduled run, a run labelled `backfill`, a backfill-labelled first run, a run released from a hold,
an event no run wrote, a non-public event), the events the list returns with `genuine=true` are the
events `services.trends.snapshot.window_events` counts for that day, and `genuine=false` the ones it
leaves out; and the rule's name here is the recorder's, so renaming the rule there fails here.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api import opportunity_relevance
from services.api.change_rule import GENUINE_CHANGE_RULE, TO_STATES, event_to_state
from services.api.conftest import (
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.ratelimit import default_limiter
from services.db.models import (
    LIFECYCLE_STATES,
    OPPORTUNITY_STATUSES,
    Event,
    Opportunity,
    Proposal,
    Source,
    SourceRun,
)
from services.trends import snapshot

UTC = dt.UTC


@dataclass
class World:
    today: dt.datetime
    pub: Source
    gb: Source
    proposals: dict[str, Proposal] = field(default_factory=dict)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    runs: dict[str, SourceRun] = field(default_factory=dict)


def _run(db: Session, source: Source, started: dt.datetime, **kw: Any) -> SourceRun:
    run = SourceRun(source_id=source.id, started_at=started, status=kw.pop("status", "ok"), **kw)
    db.add(run)
    db.flush()
    return run


def _event(
    db: Session,
    subject: Proposal | Opportunity,
    source: Source,
    observed: dt.datetime,
    event_type: str,
    run: SourceRun | None,
    *,
    after: dict[str, Any] | None = None,
    public: bool = True,
) -> Event:
    ev = Event(
        subject_type="proposal" if isinstance(subject, Proposal) else "opportunity",
        subject_id=subject.id,
        event_type=event_type,
        observed_at=observed,
        published_at=observed + dt.timedelta(minutes=5) if public else None,
        public_at=observed + dt.timedelta(minutes=5) if public else None,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=observed,
        licence_id=source.licence_id,
        after=after,
        changed_keys=["lifecycle_state"] if after else [],
        run_id=run.id if run else None,
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )
    db.add(ev)
    db.flush()
    return ev


def build_world(db: Session) -> World:
    today = dt.datetime.combine(dt.datetime.now(UTC).date(), dt.time(0), tzinfo=UTC)
    lic = make_open_licence(db)
    w = World(
        today=today,
        pub=make_public_source(db, lic, id_="us.test.queue"),
        gb=make_public_source(db, lic, id_="gb.test.register"),
    )
    for i, key in enumerate(("solar", "wind", "bess", "line", "dc")):
        w.proposals[key] = make_visible_proposal(db, w.pub, public_id_suffix=str(i + 1))
    w.opportunities["rfp"] = make_visible_opportunity(db, w.pub, public_id_suffix="1")
    janitorial = make_visible_opportunity(db, w.pub, public_id_suffix="2")
    janitorial.title, janitorial.technologies = "Janitorial services for the town hall", []
    w.opportunities["janitorial"] = janitorial
    day = dt.timedelta(days=1)
    # Public since before the window, so the recorder's day-by-day reads see what the list sees now.
    for subject in (*w.proposals.values(), *w.opportunities.values()):
        subject.public_at = today - 30 * day
    w.runs = {
        "first": _run(db, w.pub, today - 6 * day),
        "regular": _run(db, w.pub, today - 4 * day, trigger="schedule"),
        "backfill": _run(db, w.pub, today - 3 * day, trigger="backfill"),
        "released": _run(db, w.pub, today - 2 * day, released_at=today - 2 * day + dt.timedelta(hours=2)),
        "failed": _run(db, w.pub, today - 7 * day, status="failed"),
        "gb_first": _run(db, w.gb, today - 2 * day, trigger="backfill"),
        "gb_second": _run(db, w.gb, today - day),
    }
    r, p, o = w.runs, w.proposals, w.opportunities
    at = dt.timedelta(hours=6)
    # The first promoted run of the source: a seed, all new to the store.
    _event(
        db, p["solar"], w.pub, today - 6 * day + at, "created", r["first"], after={"lifecycle_state": "filed"}
    )
    _event(
        db, o["rfp"], w.pub, today - 6 * day + at, "created", r["first"], after={"lifecycle_state": "open"}
    )
    # Later runs: genuine changes, the backfill-labelled one too.
    _event(
        db,
        p["solar"],
        w.pub,
        today - 4 * day + at,
        "status_change",
        r["regular"],
        after={"lifecycle_state": "studied"},
    )
    _event(
        db,
        p["wind"],
        w.pub,
        today - 4 * day + at,
        "created",
        r["regular"],
        after={"lifecycle_state": "filed"},
    )
    _event(
        db,
        p["bess"],
        w.pub,
        today - 4 * day + at,
        "withdrawn",
        r["regular"],
        after={"lifecycle_state": "withdrawn"},
    )
    _event(
        db,
        o["rfp"],
        w.pub,
        today - 4 * day + at,
        "status_change",
        r["regular"],
        after={"lifecycle_state": "closed"},
    )
    _event(
        db,
        o["janitorial"],
        w.pub,
        today - 4 * day + at,
        "created",
        r["regular"],
        after={"lifecycle_state": "open"},
    )
    _event(
        db,
        p["dc"],
        w.pub,
        today - 3 * day + at,
        "created",
        r["backfill"],
        after={"lifecycle_state": "announced"},
    )
    _event(
        db,
        p["solar"],
        w.pub,
        today - 3 * day + at,
        "status_change",
        r["backfill"],
        after={"lifecycle_state": "built"},
    )
    _event(
        db,
        p["wind"],
        w.pub,
        today - 3 * day + at,
        "delisted",
        r["backfill"],
        after={"register_name": "ERCOT"},
    )
    _event(db, p["line"], w.pub, today - 3 * day + at, "capacity_changed", r["backfill"])
    # A released hold, a backfill-labelled first run of another source and its second run.
    _event(
        db,
        p["line"],
        w.pub,
        today - 2 * day + at,
        "created",
        r["released"],
        after={"lifecycle_state": "filed"},
    )
    _event(
        db,
        p["bess"],
        w.gb,
        today - 2 * day + at,
        "created",
        r["gb_first"],
        after={"lifecycle_state": "filed"},
    )
    _event(
        db,
        p["bess"],
        w.gb,
        today - day + at,
        "status_change",
        r["gb_second"],
        after={"lifecycle_state": "permitted"},
    )
    # No source run (a merge, an edit), and a removal no public surface serves.
    _event(db, p["dc"], w.pub, today - day + at, "status_change", None, after={"lifecycle_state": "filed"})
    _event(db, p["dc"], w.pub, today - day + at, "removed_from_source", r["regular"], public=False)
    db.commit()
    return w


@pytest.fixture()
def world(db: Session) -> World:
    return build_world(db)


def _rows(client: TestClient, params: Mapping[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        default_limiter.reset()
        resp = client.get(
            "/v1/events", params={**params, "limit": "200", **({"cursor": cursor} if cursor else {})}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows.extend(body["data"])
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            return rows


def _total(client: TestClient, params: Mapping[str, str]) -> int:
    default_limiter.reset()
    resp = client.get("/v1/events", params={**params, "limit": "1", "include": "count"})
    assert resp.status_code == 200, resp.text
    return int(resp.json()["meta"]["total"])


def _day(day: dt.datetime) -> dict[str, str]:
    end = day + dt.timedelta(days=1) - dt.timedelta(microseconds=1)
    return {"observed_at[from]": day.isoformat(), "observed_at[to]": end.isoformat()}


def _listed(
    client: TestClient, ids: Mapping[str, Any], params: Mapping[str, str]
) -> Counter[tuple[Any, ...]]:
    """The events the list returns for `params`, keyed as the recorder's rows are."""
    return Counter(
        (r["subject_type"], ids[r["subject_id"]], r["event_type"], r["provenance"]["source_id"])
        for r in _rows(client, params)
    )


def test_the_rule_is_the_recorders(client: TestClient, world: World, db: Session) -> None:
    assert GENUINE_CHANGE_RULE == snapshot.ACTIVITY_RULE
    ids = {
        **{p.public_id: p.id for p in world.proposals.values()},
        **{o.public_id: o.id for o in world.opportunities.values()},
    }
    counted_total = left_total = 0
    for back in range(7, 0, -1):
        day = world.today - dt.timedelta(days=back)
        # The recorder's document for day D + 1 holds D's activity (`activity_window`).
        recorded = snapshot.window_events(db, day + dt.timedelta(days=1, hours=5, minutes=58))
        counted = Counter(
            (e.subject_type, e.subject_id, e.event_type, e.source_id) for e in recorded if e.reason is None
        )
        left_out = Counter(
            (e.subject_type, e.subject_id, e.event_type, e.source_id)
            for e in recorded
            if e.reason is not None
        )

        assert _listed(client, ids, {**_day(day), "genuine": "true"}) == counted, day.date()
        assert _listed(client, ids, {**_day(day), "genuine": "false"}) == left_out, day.date()
        assert _total(client, {**_day(day), "genuine": "true"}) == sum(counted.values())
        counted_total += sum(counted.values())
        left_total += sum(left_out.values())
    # Not vacuous: ten genuine changes, five left out (two first-load, one first-load from a backfill-
    # labelled first run, one released hold, one with no run); the removal is served by neither.
    assert (counted_total, left_total) == (10, 5)


def test_genuine_true_and_false_partition_the_list(client: TestClient, world: World) -> None:
    every = _total(client, {})
    assert _total(client, {"genuine": "true"}) + _total(client, {"genuine": "false"}) == every == 15


def test_to_state_reads_the_state_moved_to(client: TestClient, world: World) -> None:
    assert {r["after"]["lifecycle_state"] for r in _rows(client, {"to_state": "built,withdrawn"})} == {
        "built",
        "withdrawn",
    }
    assert _total(client, {"to_state": "open"}) == 2
    assert _total(client, {"to_state": "open", "genuine": "true"}) == 1
    # A departure and a capacity change carry no state, so no value selects them.
    every_state = ",".join(TO_STATES)
    assert _total(client, {}) - _total(client, {"to_state": every_state}) == 2
    assert set(TO_STATES) == set(LIFECYCLE_STATES) | set(OPPORTUNITY_STATUSES)
    bad = client.get("/v1/events", params={"to_state": "approved"})
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "to_state"


def test_relevance_keeps_only_energy_notices(client: TestClient, world: World) -> None:
    rows = _rows(client, {"relevance": "energy"})
    assert {r["subject_id"] for r in rows} == {world.opportunities["rfp"].public_id}
    assert _total(client, {"relevance": "energy"}) == len(rows) == 2
    assert _total(client, {"relevance": "all"}) == _total(client, {})
    assert _total(client, {"relevance": "energy", "subject_type": "proposal"}) == 0
    assert client.get("/v1/events", params={"relevance": "most"}).status_code == 400


def test_a_withdrawal_that_names_no_state_moved_to_withdrawn(
    client: TestClient, world: World, db: Session
) -> None:
    """The loader writes a row that left a register declaring `removal_meaning = "withdrawn"` as a
    `withdrawn` event with `before` holding the last state and no `after` (`_load_one_event`)."""
    before = _total(client, {"to_state": "withdrawn"})
    filed = _total(client, {"to_state": "filed"})
    ev = _event(
        db, world.proposals["line"], world.pub, world.today - dt.timedelta(hours=3), "withdrawn", None
    )
    ev.before = {"lifecycle_state": "filed"}
    db.commit()
    assert _total(client, {"to_state": "withdrawn"}) == before + 1
    assert _total(client, {"to_state": "withdrawn", "subject_type": "proposal"}) == before + 1
    # It names no other state (its `before` is where it came from), and an opportunity's event of
    # that type is not read as one.
    assert _total(client, {"to_state": "filed"}) == filed
    assert event_to_state("opportunity", "withdrawn", None) is None
    assert event_to_state("proposal", "withdrawn", {"lifecycle_state": "cancelled"}) == "cancelled"


def test_relevance_binds_no_parameter_per_notice(
    client: TestClient, world: World, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 2026-10-23: the notices' ids were one parameter each, so the list failed once they
    passed the driver's limit (SQLite's compiled limit, 32,766 by default and 250,000 in this
    environment's build; psycopg's 65,535). Here the connection's limit is lowered
    to 100 before any request (so no cached answer stands in for one) and 150 energy notices each have
    an event; both lists answer with every one of them."""
    for i in range(150):
        notice = make_visible_opportunity(db, world.pub, public_id_suffix=f"bulk{i}")
        notice.public_at = world.today - dt.timedelta(days=30)
        _event(db, notice, world.pub, world.today - dt.timedelta(hours=2), "created", world.runs["regular"])
    db.commit()
    monkeypatch.setattr(opportunity_relevance, "BOUND_IDS_MAX", 20)
    raw = db.connection().connection.driver_connection
    assert isinstance(raw, sqlite3.Connection)
    previous = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
    try:
        # The world's two events of its one energy notice, and one each for the 150 more.
        assert _total(client, {"relevance": "energy"}) == 152
        assert _count(client, "/v1/opportunities", {"relevance": "energy"}) == 151
        page = _get_page(client, {"relevance": "energy", "limit": "20"})
        assert len(page) == 20 and all(r["subject_type"] == "opportunity" for r in page)
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
    # The bound form answers the same.
    monkeypatch.setattr(opportunity_relevance, "BOUND_IDS_MAX", 2_000)
    assert _total(client, {"relevance": "energy", "subject_type": "opportunity"}) == 152


def _count(client: TestClient, path: str, params: Mapping[str, str]) -> int:
    default_limiter.reset()
    resp = client.get(path, params={**params, "limit": "1", "include": "count"})
    assert resp.status_code == 200, resp.text
    return int(resp.json()["meta"]["total"])


def _get_page(client: TestClient, params: Mapping[str, str]) -> list[dict[str, Any]]:
    default_limiter.reset()
    resp = client.get("/v1/events", params=dict(params))
    assert resp.status_code == 200, resp.text
    rows: list[dict[str, Any]] = resp.json()["data"]
    return rows


def test_genuine_takes_true_or_false_only(client: TestClient, world: World) -> None:
    resp = client.get("/v1/events", params={"genuine": "yes"})
    assert resp.status_code == 400 and resp.json()["errors"][0]["field"] == "genuine"
    assert _total(client, {"genuine": ""}) == _total(client, {})
