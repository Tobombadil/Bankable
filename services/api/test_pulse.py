"""`GET /v1/pulse` (`services/api/front.py`): what genuinely changed in the last days.

The contract held here:

- every count is the total of the events list it names (`GET /v1/events?<list_query>&include=count`),
  by change type and by country, and `total` and the countries' totals are the sums of the lists
  they name;
- only genuine changes count: a source's first load (whatever its trigger label), a run released from
  a data-quality hold, an event no source run wrote and an event no public surface serves are never
  counted, nor a notice that is not about energy, nor a change the pulse does not report;
- the window is `days` back from now, 1 to 90;
- the items are the counted events, newest first, paged, each with its record, source and date;
- the response validates against `api/openapi.yaml`.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_attribution_licence,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.ratelimit import default_limiter
from services.db.models import Event, Opportunity, Proposal, Source, SourceRun

UTC = dt.UTC
SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


@dataclass
class World:
    now: dt.datetime
    pub: Source
    gb: Source
    p: dict[str, Proposal] = field(default_factory=dict)
    o: dict[str, Opportunity] = field(default_factory=dict)
    events: dict[str, Event] = field(default_factory=dict)


def _run(db: Session, source: Source, started: dt.datetime, **kw: Any) -> SourceRun:
    run = SourceRun(source_id=source.id, started_at=started, status=kw.pop("status", "ok"), **kw)
    db.add(run)
    db.flush()
    return run


def build_world(db: Session) -> World:
    now = dt.datetime.now(UTC)
    day = dt.timedelta(days=1)
    w = World(
        now=now,
        pub=make_public_source(db, make_open_licence(db), id_="us.test.queue"),
        gb=make_public_source(db, make_attribution_licence(db), id_="gb.test.register"),
    )
    n = 0

    def proposal(key: str, jurisdiction: str, source: Source | None = None, **kw: Any) -> None:
        nonlocal n
        n += 1
        prop = make_visible_proposal(db, source or w.pub, public_id_suffix=str(n), jurisdiction=jurisdiction)
        for name, value in kw.items():
            setattr(prop, name, value)
        prop.public_at = now - 60 * day
        w.p[key] = prop

    def opportunity(key: str, jurisdiction: str, **kw: Any) -> None:
        nonlocal n
        n += 1
        opp = make_visible_opportunity(db, w.pub, public_id_suffix=str(n))
        opp.jurisdiction = jurisdiction
        for name, value in kw.items():
            setattr(opp, name, value)
        opp.public_at = now - 60 * day
        w.o[key] = opp

    proposal("solar_tx", "US-TX", kind="generation", technology="solar", lifecycle_state="built")
    proposal("wind_ok", "US-OK", kind="generation", technology="wind", lifecycle_state="permitted")
    proposal("bess_tx", "US-TX", lifecycle_state="withdrawn")
    proposal("dc_va", "US-VA", kind="load", technology="load", lifecycle_state="under_construction")
    proposal("gb_wind", "GB", source=w.gb, kind="generation", technology="wind", lifecycle_state="contracted")
    proposal("line_tx", "US-TX", kind="transmission", technology="hvdc")
    opportunity("rfp_az", "US-AZ", technologies=["solar_pv"])
    opportunity("janitorial", "US", title="Janitorial services for the town hall", technologies=[])
    opportunity("gb_tender", "GB", kind="tender", technologies=["wind"])
    opportunity("wb_global", "global", kind="procurement_notice", technologies=["solar_pv"])
    opportunity("eu_tender", "EU", kind="tender", technologies=["bess_li_ion"])
    opportunity("closed_az", "US-AZ", technologies=["solar_pv"], status="closed")

    runs = {
        "pub_first": _run(db, w.pub, now - 40 * day, trigger="backfill"),
        "pub_old": _run(db, w.pub, now - 10 * day),
        "pub_regular": _run(db, w.pub, now - 3 * day, trigger="schedule"),
        "pub_released": _run(db, w.pub, now - 2 * day, released_at=now - 2 * day + dt.timedelta(hours=1)),
        "gb_first": _run(db, w.gb, now - 2 * day, trigger="backfill"),
        "gb_second": _run(db, w.gb, now - day),
    }

    def event(
        key: str,
        subject: Proposal | Opportunity,
        event_type: str,
        run: str | None,
        *,
        to: str | None = None,
        source: Source | None = None,
        at: dt.datetime | None = None,
        public: bool = True,
        **kw: Any,
    ) -> None:
        source = source or w.pub
        observed = at or (runs[run].started_at + dt.timedelta(minutes=len(w.events)) if run else now - day)
        ev = Event(
            subject_type="proposal" if isinstance(subject, Proposal) else "opportunity",
            subject_id=subject.id,
            event_type=event_type,
            observed_at=observed,
            published_at=observed + dt.timedelta(minutes=5) if public else None,
            public_at=observed + dt.timedelta(minutes=5) if public else None,
            source_id=source.id,
            source_url=f"{source.url}#{key}",
            retrieved_at=observed,
            licence_id=source.licence_id,
            after={"lifecycle_state": to} if to else kw.pop("after", None),
            before=kw.pop("before", None),
            reason=kw.pop("reason", None),
            changed_keys=["lifecycle_state"] if to else [],
            run_id=runs[run].id if run else None,
            idempotency_key=f"test:{uuid.uuid4().hex}",
        )
        db.add(ev)
        db.flush()
        w.events[key] = ev

    p, o = w.p, w.o
    # Genuine changes in the last seven days.
    event("new_solar", p["solar_tx"], "created", "pub_regular", to="filed")
    event(
        "permitted",
        p["wind_ok"],
        "status_change",
        "pub_regular",
        to="permitted",
        before={"lifecycle_state": "studied"},
    )
    event("withdrawn", p["bess_tx"], "withdrawn", "pub_regular", to="withdrawn")
    event("construction", p["dc_va"], "status_change", "pub_regular", to="under_construction")
    event("energised", p["solar_tx"], "status_change", "pub_regular", to="built")
    event(
        "delisted",
        p["wind_ok"],
        "delisted",
        "pub_regular",
        after={"register_name": "ERCOT"},
        reason="No longer in ERCOT's report (reason not stated)",
    )
    event("rfp_opened", o["rfp_az"], "created", "pub_regular", to="open")
    event("wb_opened", o["wb_global"], "status_change", "pub_regular", to="open")
    event("eu_opened", o["eu_tender"], "created", "pub_regular", to="open")
    event("gb_contracted", p["gb_wind"], "status_change", "gb_second", to="contracted", source=w.gb)
    event("gb_opened", o["gb_tender"], "created", "gb_second", to="open", source=w.gb)
    # Changes the pulse does not report, and changes that are not genuine.
    event("capacity", p["dc_va"], "capacity_changed", "pub_regular")
    event("closed", o["closed_az"], "status_change", "pub_regular", to="closed")
    event("janitorial", o["janitorial"], "created", "pub_regular", to="open")
    event("first_load", p["line_tx"], "created", "pub_first", to="filed")
    event("gb_first_load", p["gb_wind"], "created", "gb_first", to="filed", source=w.gb)
    event("released", p["line_tx"], "status_change", "pub_released", to="studied")
    event("no_run", p["line_tx"], "status_change", None, to="permitted")
    event("removed", p["line_tx"], "removed_from_source", "pub_regular", public=False)
    # Ten days ago: inside `days=30`, outside the default week.
    event("old", p["solar_tx"], "status_change", "pub_old", to="studied")
    db.commit()
    return w


@pytest.fixture()
def world(db: Session) -> World:
    return build_world(db)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return loaded


def _get(client: TestClient, path: str, params: Mapping[str, str]) -> dict[str, Any]:
    default_limiter.reset()
    resp = client.get(path, params=dict(params))
    assert resp.status_code == 200, (path, params, resp.text)
    body: dict[str, Any] = resp.json()
    return body


def pulse(client: TestClient, **params: str) -> dict[str, Any]:
    return _get(client, "/v1/pulse", params)


def list_count(client: TestClient, ref: dict[str, Any]) -> int:
    assert ref["path"] == "/v1/events"
    return int(_get(client, ref["path"], {**ref["query"], "limit": "1", "include": "count"})["meta"]["total"])


def list_subjects(client: TestClient, ref: dict[str, Any]) -> set[str]:
    body = _get(client, ref["path"], {**ref["query"], "limit": "200"})
    assert not body["page"]["has_more"]
    return {r["subject_id"] for r in body["data"]}


def _counts(data: dict[str, Any]) -> dict[str, int]:
    return {
        f"{c['type']}:{c['to_state']}" if c["type"] == "status_change" else c["type"]: c["events"]
        for c in data["changes"]
        if c["events"]
    }


PARAMS: tuple[dict[str, str], ...] = (
    {},
    {"country": "US"},
    {"country": "GB"},
    {"country": "us,gb"},
    {"country": "EU"},
    {"days": "30"},
    {"days": "1"},
)


def test_every_pulse_figure_is_the_total_of_the_list_it_names(client: TestClient, world: World) -> None:
    checked = 0
    for params in PARAMS:
        data = pulse(client, **params)["data"]
        for change in data["changes"]:
            assert list_count(client, change["list_query"]) == change["events"], (params, change["type"])
            assert len(list_subjects(client, change["list_query"])) == change["subjects"]
            for entry in change["by_country"]:
                if entry["list_query"] is None:
                    assert entry["country"] is None
                    continue
                assert list_count(client, entry["list_query"]) == entry["events"], (params, entry)
            assert sum(e["events"] for e in change["by_country"]) == change["events"]
            checked += 1
        assert data["total"]["events"] == sum(
            list_count(client, ref) for ref in data["total"]["list_queries"]
        )
        for entry in data["by_country"]:
            if entry["country"] is not None:
                assert entry["events"] == sum(list_count(client, ref) for ref in entry["list_queries"])
        assert sum(e["events"] for e in data["by_country"]) == data["total"]["events"]
    # New proposals, a status change to each of ten states (not `unknown`; `postponed` since
    # 2026-10-24), departures, notices.
    assert checked == len(PARAMS) * 13


def test_only_genuine_reported_changes_are_counted(client: TestClient, world: World) -> None:
    data = pulse(client)["data"]
    assert _counts(data) == {
        "new_proposal": 1,
        "status_change:permitted": 1,
        "status_change:contracted": 1,
        "status_change:under_construction": 1,
        "status_change:built": 1,
        "status_change:withdrawn": 1,
        "delisted": 1,
        "new_open_opportunity": 4,
    }
    # Eleven events changed nine records: the Texas solar project was listed and energised, and the
    # Oklahoma wind project was permitted and then left ERCOT's report.
    assert (data["total"]["events"], data["total"]["subjects"]) == (11, 9)
    counted = {item["event_id"] for item in data["items"]}
    assert len(counted) == 11
    left_out = (
        "capacity",
        "closed",
        "janitorial",
        "first_load",
        "gb_first_load",
        "released",
        "no_run",
        "removed",
        "old",
    )
    served = _get(client, "/v1/events", {"limit": "200"})["data"]
    ids = {e["id"] for e in served}
    for key in left_out:
        event_id = _public_event_id(world.events[key])
        assert event_id not in counted, key
        assert (event_id in ids) is (key != "removed"), key


def _public_event_id(event: Event) -> str:
    from services.ids import public_id

    return public_id("evt", event.id)


def test_counts_by_country_read_each_records_jurisdiction(client: TestClient, world: World) -> None:
    data = pulse(client)["data"]
    totals = {e["country"]: e["events"] for e in data["by_country"]}
    assert totals == {"US": 7, "GB": 2, "EU": 1, None: 1}
    opened = next(c for c in data["changes"] if c["type"] == "new_open_opportunity")
    assert {e["country"]: e["events"] for e in opened["by_country"]} == {"US": 1, "GB": 1, "EU": 1, None: 1}
    us = pulse(client, country="US")["data"]
    assert us["country"] == ["US"] and us["total"]["events"] == 7
    assert {e["country"] for e in us["by_country"]} == {"US"}
    both = pulse(client, country="us,gb")["data"]
    assert both["country"] == ["GB", "US"] and both["total"]["events"] == 9


def test_the_window_is_days_back_from_now(client: TestClient, world: World) -> None:
    week = pulse(client)["data"]
    assert week["window"]["days"] == 7
    start = dt.datetime.fromisoformat(week["window"]["from"])
    end = dt.datetime.fromisoformat(week["window"]["to"])
    assert end - start == dt.timedelta(days=7)
    assert abs((end - dt.datetime.now(UTC)).total_seconds()) < 60
    month = pulse(client, days="30")["data"]
    assert month["total"]["events"] == 12
    assert _counts(month)["status_change:studied"] == 1  # the change ten days ago
    assert pulse(client, days="1")["data"]["total"]["events"] == 2  # the Great Britain run yesterday
    for bad in ("0", "91", "seven", "-1"):
        resp = client.get("/v1/pulse", params={"days": bad})
        assert resp.status_code == 400 and resp.json()["errors"][0]["field"] == "days", bad
    for params in ({"country": "Britain"}, {"genuine": "true"}, {"include": "count"}, {"limit": "500"}):
        assert client.get("/v1/pulse", params=params).status_code == 400, params


def test_items_are_the_counted_events_newest_first_with_record_source_and_date(
    client: TestClient, world: World
) -> None:
    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        body = pulse(client, limit="4", **({"cursor": cursor} if cursor else {}))
        seen.extend(body["data"]["items"])
        assert body["data"]["total"]["events"] == 11  # every page carries the whole count
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            break
    assert len(seen) == len({i["event_id"] for i in seen}) == 11
    times = [i["observed_at"] for i in seen]
    assert times == sorted(times, reverse=True)
    for item in seen:
        record, source = item["record"], item["provenance"]
        assert record["url"].startswith("https://") and record["public_id"].startswith(("prop_", "opp_"))
        assert source["source_id"] in ("us.test.queue", "gb.test.register")
        assert (
            source["source_url"] and source["retrieved_at"] and source["licence_id"] and source["reuse_class"]
        )
    permitted = next(i for i in seen if i["to_state"] == "permitted")
    assert permitted["type"] == "status_change" and permitted["from_state"] == "studied"
    assert permitted["record"]["name"] == world.p["wind_ok"].name_canonical
    assert permitted["record"]["country"] == "US" and permitted["record"]["lifecycle_state"] == "permitted"
    delisted = next(i for i in seen if i["type"] == "delisted")
    assert (
        delisted["reason"] == "No longer in ERCOT's report (reason not stated)"
        and delisted["to_state"] is None
    )
    opened = [i for i in seen if i["type"] == "new_open_opportunity"]
    assert {i["record"]["country"] for i in opened} == {"US", "GB", "EU", None}
    assert all(i["record"]["status"] == "open" for i in opened)
    bad = client.get("/v1/pulse", params={"cursor": "not-a-cursor"})
    assert bad.status_code == 400


def test_the_credit_names_every_counted_source(client: TestClient, world: World) -> None:
    body = pulse(client)
    credited = {s["source_id"]: s["record_count"] for s in body["licence_summary"]["sources"]}
    assert credited == {"us.test.queue": 9, "gb.test.register": 2}


def _later_event(db: Session, w: World, subject: Proposal, event_type: str, **kw: Any) -> Event:
    """A change event from a run two days ago (after the source's first run, so a genuine one)."""
    observed = w.now - dt.timedelta(days=2)
    run = _run(db, w.pub, observed - dt.timedelta(minutes=30), trigger="schedule")
    ev = Event(
        subject_type="proposal",
        subject_id=subject.id,
        event_type=event_type,
        observed_at=observed,
        published_at=observed,
        public_at=observed,
        source_id=w.pub.id,
        source_url=w.pub.url,
        retrieved_at=observed,
        licence_id=w.pub.licence_id,
        run_id=run.id,
        idempotency_key=f"test:{uuid.uuid4().hex}",
        **kw,
    )
    db.add(ev)
    db.commit()
    return ev


def test_a_withdrawal_that_names_no_state_is_a_move_to_withdrawn(
    client: TestClient, world: World, db: Session
) -> None:
    """The loader's shape for a row that left a register declaring `removal_meaning = "withdrawn"`:
    `before` holds the last state, `after` is empty (review of 2026-10-23: it was in no change type)."""
    ev = _later_event(
        db, world, world.p["line_tx"], "withdrawn", before={"lifecycle_state": "filed"}, after=None
    )
    data = pulse(client)["data"]
    withdrawn = next(c for c in data["changes"] if c["to_state"] == "withdrawn")
    assert withdrawn["events"] == 2 and list_count(client, withdrawn["list_query"]) == 2
    assert data["total"]["events"] == 12
    item = next(i for i in data["items"] if i["event_id"] == _public_event_id(ev))
    assert (item["type"], item["to_state"], item["from_state"]) == ("status_change", "withdrawn", "filed")


def test_a_move_to_unknown_is_listed_but_not_reported(client: TestClient, world: World, db: Session) -> None:
    _later_event(db, world, world.p["wind_ok"], "status_change", after={"lifecycle_state": "unknown"})
    data = pulse(client)["data"]
    assert "unknown" not in {c["to_state"] for c in data["changes"]}
    assert data["total"]["events"] == 11
    window = {"observed_at[from]": data["window"]["from"], "observed_at[to]": data["window"]["to"]}
    ref = {"path": "/v1/events", "query": {**window, "genuine": "true", "to_state": "unknown"}}
    assert list_count(client, ref) == 1


def test_an_event_counted_under_a_country_its_record_is_not_served_is_noted(
    client: TestClient, world: World, db: Session
) -> None:
    """A record whose stored jurisdiction came from a source the tier may not read is counted under
    the stored country (the list's `country=` reads it) but served another; the pulse says how many."""
    hidden = make_public_source(db, world.pub.licence, id_="us.test.ingest_only")
    hidden.publish_state = "ingest_only"
    subject = world.p["solar_tx"]
    public_link = next(link for link in subject.sources if link.source_id == world.pub.id)
    public_link.normalised = {"jurisdiction": "MX", "lifecycle_state": "built"}
    subject.field_provenance = {"jurisdiction": {"source_id": hidden.id, "licence_id": hidden.licence_id}}
    db.commit()
    data = pulse(client)["data"]
    # The Texas project's two events this week (listed, energised) are counted under US ...
    assert data["served_not_stored"] == {"events": 2, "subjects": 1}
    assert {e["country"]: e["events"] for e in data["by_country"]}["US"] == 7
    # ... and its record is served with the public register's jurisdiction.
    served = {i["record"]["country"] for i in data["items"] if i["record"]["public_id"] == subject.public_id}
    assert served == {"MX"}
    assert pulse(client, country="GB")["data"]["served_not_stored"] == {"events": 0, "subjects": 0}


def test_the_pulse_response_matches_its_schema(
    client: TestClient, world: World, spec: dict[str, Any]
) -> None:
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    schema = spec["components"]["schemas"]["PulseResponse"]
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    for params in ({}, {"country": "US", "limit": "2"}, {"days": "90"}):
        errors = sorted(validator.iter_errors(pulse(client, **params)), key=str)
        assert not errors, [f"{e.message} at {list(e.absolute_path)}" for e in errors]
