"""The daily metrics snapshot (`services/trends/snapshot.py`), on a store built to hold one of every
case the public tier treats differently.

The contract held here:

- the five proposal blocks partition the visible set, and nothing the public tier may not serve is
  counted: a restricted register's row, a row only an off-surface register lists, a row not yet
  public, an unpublished row;
- every figure with a list filter is the `include=count` total of the public list query the
  document names for it, read through the real app, by US state and by state and kind too, so a
  state's landing figures (`/texas`) rebuild from the document alone; a figure differs from its
  query only by the records `visibility.served_not_stored` counts;
- capacity is summed by the kind's meaning (generation and storage, demand apart, lines never),
  and a value a withheld register supplied is counted as served, never as stored;
- the activity counts only genuine changes, events and the distinct subjects they changed, and
  says what it left out and why; a run's trigger label is not a reason;
- a naive `now` is refused;
- the document names its sources with their licences and the posture it was computed under, and
  the computation reads no table but the public record's.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.ratelimit import default_limiter
from services.api.visibility import PUBLISHABLE_REUSE_CLASSES
from services.db.base import Base
from services.db.models import (
    Event,
    Licence,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    Source,
    SourceRun,
)
from services.trends import snapshot
from services.trends.snapshot import NULL_KEY, activity_window, compute_snapshot, figure
from services.trends.store import document_bytes

UTC = dt.UTC


def _licence(db: Session, id_: str, reuse_class: str) -> Licence:
    lic = Licence(
        id=id_,
        name=f"{reuse_class} licence",
        reuse_class=reuse_class,
        allows_derived_publication=reuse_class != "restricted",
        allows_raw_publication=reuse_class == "open",
        allows_api_redistribution=reuse_class == "open",
        allows_bulk_export=reuse_class == "open",
        allows_commercial_use=reuse_class == "open",
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def _source(db: Session, id_: str, licence: Licence, publish_state: str = "public") -> Source:
    src = make_public_source(db, licence, id_=id_)
    src.publish_state = publish_state
    src.name = f"Register {id_}"
    db.flush()
    return src


@dataclass
class World:
    now: dt.datetime
    pub: Source
    pub_gb: Source
    restricted: Source
    staged: Source
    proposals: dict[str, Proposal] = field(default_factory=dict)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    runs: dict[str, SourceRun] = field(default_factory=dict)
    _n: int = 0

    def proposal(
        self,
        db: Session,
        key: str,
        *,
        kind: str = "generation",
        technology: str | None = "solar",
        state: str = "filed",
        capacity: float | None = 100.0,
        storage_mwh: float | None = None,
        jurisdiction: str = "US-TX",
        iso: str | None = "ERCOT",
        source: Source | None = None,
    ) -> Proposal:
        self._n += 1
        prop = make_visible_proposal(
            db,
            source or self.pub,
            public_id_suffix=str(self._n),
            lifecycle_state=state,
            technology=technology or "solar",
            jurisdiction=jurisdiction,
        )
        prop.kind, prop.technology, prop.iso = kind, technology, iso
        prop.capacity_mw, prop.storage_mwh = capacity, storage_mwh
        db.flush()
        self.proposals[key] = prop
        return prop

    def opportunity(
        self,
        db: Session,
        key: str,
        *,
        status: str = "open",
        kind: str = "rfp",
        title: str | None = None,
        technologies: list[str] | None = None,
        jurisdiction: str = "US-AZ",
    ) -> Opportunity:
        self._n += 1
        opp = make_visible_opportunity(db, self.pub, public_id_suffix=str(self._n), status=status)
        opp.kind, opp.jurisdiction = kind, jurisdiction
        if title is not None:
            opp.title = title
        if technologies is not None:
            opp.technologies = technologies
        db.flush()
        self.opportunities[key] = opp
        return opp


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
    event_type: str = "status_change",
    run: SourceRun | None = None,
    *,
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
        run_id=run.id if run else None,
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )
    db.add(ev)
    db.flush()
    return ev


def build_world(db: Session) -> World:
    now = dt.datetime.now(UTC)
    open_lic = make_open_licence(db)
    w = World(
        now=now,
        pub=_source(db, "us.test.queue", open_lic),
        pub_gb=_source(db, "gb.test.register", _licence(db, "attr-lic", "attribution")),
        restricted=_source(db, "us.test.restricted", _licence(db, "restricted-lic", "restricted")),
        staged=_source(db, "us.test.staged", open_lic, publish_state="ingest_only"),
    )
    # Active, listed: the map's default view.
    w.proposal(db, "solar_tx", capacity=200.0)
    w.proposal(db, "wind_ok", technology="wind", state="permitted", jurisdiction="US-OK", iso="SPP")
    w.proposal(
        db,
        "bess_tx",
        kind="storage",
        technology="bess_li_ion",
        state="studied",
        storage_mwh=400.0,
        iso="ERCO",
    )
    w.proposal(
        db, "dc_va", kind="load", technology="load", state="announced", capacity=300.0, jurisdiction="US-VA"
    )
    w.proposal(
        db, "line_tx", kind="transmission", technology=None, state="under_construction", capacity=1500.0
    )
    w.proposal(db, "solar_nomw", capacity=None, state="contracted", iso=None)
    w.proposal(db, "gb_wind", technology="wind", jurisdiction="GB", iso=None, source=w.pub_gb)
    w.proposal(db, "mx_solar", jurisdiction="MX", iso=None)
    w.proposal(db, "us_only", jurisdiction="US", iso="MISO")
    w.proposal(
        db, "dc_tx", kind="load", technology="load", state="announced", capacity=500.0, jurisdiction="US-TX"
    )
    # Active state, but no register lists it any more: hidden by the map, still in the `/texas`
    # landing's large-load figure, which does not hide them.
    for key, kind, technology, capacity in (
        ("delisted", "generation", "solar", 50.0),
        ("delisted_load", "load", "load", 250.0),
    ):
        delisted = w.proposal(db, key, kind=kind, technology=technology, state="announced", capacity=capacity)
        link = db.query(ProposalSource).filter(ProposalSource.proposal_id == delisted.id).one()
        link.gone_at = now - dt.timedelta(days=3)
    # Outside the active view: hidden nothing.
    w.proposal(db, "withdrawn", state="withdrawn", iso="CAISO", jurisdiction="US-CA")
    w.proposal(db, "cancelled", state="cancelled", technology="wind")
    w.proposal(db, "built", state="built", capacity=80.0)
    w.proposal(db, "unknown", state="unknown")
    # Never counted.
    w.proposal(db, "restricted_only", capacity=7000.0, source=w.restricted)
    w.proposal(db, "staged_only", capacity=7000.0, source=w.staged)
    w.proposal(db, "future", capacity=7000.0).public_at = now + dt.timedelta(days=1)
    w.proposal(db, "unpublished", capacity=7000.0).publish_state = "unpublished"
    # Visible through the public register, its stored capacity from the off-surface one: served 50.
    mixed = w.proposal(db, "mixed", capacity=999.0)
    mixed_link = db.query(ProposalSource).filter(ProposalSource.proposal_id == mixed.id).one()
    mixed_link.normalised = {
        "capacity_mw": 50.0,
        "lifecycle_state": "filed",
        "technology": "solar",
        "kind": "generation",
        "jurisdiction": "US-TX",
        "iso": "ERCOT",
    }
    db.add(
        ProposalSource(
            proposal_id=mixed.id,
            source_id=w.staged.id,
            source_record_id="S-mixed",
            source_url=w.staged.url,
            retrieved_at=now - dt.timedelta(days=1),
            licence_id=w.staged.licence_id,
            normalised={"capacity_mw": 999.0},
            first_seen=now - dt.timedelta(days=2),
            last_seen=now - dt.timedelta(days=1),
        )
    )
    mixed.field_provenance = {"capacity_mw": {"source_id": w.staged.id, "licence_id": w.staged.licence_id}}
    mixed.source_count = 2

    w.opportunity(db, "rfp_solar", technologies=["solar_pv"])
    w.opportunity(db, "janitorial", title="Janitorial services for the town hall", technologies=[])
    w.opportunity(db, "grant_gb", kind="grant", jurisdiction="GB", technologies=["wind"])
    w.opportunity(db, "announced_rfp", status="announced", technologies=["bess_li_ion"])
    w.opportunity(db, "closed_rfp", status="closed")
    w.opportunity(db, "rfp_tx", jurisdiction="US-TX", technologies=["wind"])
    # Visible through the public register, its stored kind from the off-surface one: served as the
    # public register states it (a grant), listed by `kind=` as stored (an RFP).
    mixed_opp = w.opportunity(db, "mixed_opp", technologies=["solar_pv"])
    opp_link = db.query(OpportunitySource).filter(OpportunitySource.opportunity_id == mixed_opp.id).one()
    opp_link.normalised = {
        "kind": "grant",
        "title": mixed_opp.title,
        "jurisdiction": "US-AZ",
        "technologies": ["solar_pv"],
        "status": "open",
    }
    db.add(
        OpportunitySource(
            opportunity_id=mixed_opp.id,
            source_id=w.staged.id,
            source_record_id="S-mixed-opp",
            source_url=w.staged.url,
            retrieved_at=now - dt.timedelta(days=1),
            licence_id=w.staged.licence_id,
            normalised={"kind": "rfp"},
            first_seen=now - dt.timedelta(days=2),
            last_seen=now - dt.timedelta(days=1),
        )
    )
    mixed_opp.field_provenance = {"kind": {"source_id": w.staged.id, "licence_id": w.staged.licence_id}}
    mixed_opp.source_count = 2

    # Activity: the previous UTC day, and the runs the events came from.
    start, end = activity_window(now)
    first = _run(db, w.pub, start - dt.timedelta(days=10))
    regular = _run(db, w.pub, start - dt.timedelta(days=1), trigger="schedule")
    backfill = _run(db, w.pub, start, trigger="backfill")
    released = _run(db, w.pub, start, released_at=start + dt.timedelta(hours=2))
    gb_first = _run(db, w.pub_gb, start + dt.timedelta(hours=1))
    w.runs = {"first": first, "regular": regular, "backfill": backfill, "released": released}
    at = start + dt.timedelta(hours=6)
    p, o = w.proposals, w.opportunities
    _event(db, p["solar_tx"], w.pub, at, "status_change", regular)
    # A second change to the same project the same day: two events, one subject.
    _event(db, p["solar_tx"], w.pub, at + dt.timedelta(hours=1), "status_change", regular)
    _event(db, p["wind_ok"], w.pub, at, "capacity_changed", regular)
    _event(db, p["withdrawn"], w.pub, at, "withdrawn", regular)
    _event(db, o["rfp_solar"], w.pub, at, "created", regular)
    _event(db, p["bess_tx"], w.pub, at, "created", first)
    _event(db, p["gb_wind"], w.pub_gb, at, "created", gb_first)
    _event(db, p["dc_va"], w.pub, at, "created", backfill)
    _event(db, p["line_tx"], w.pub, at, "created", released)
    _event(db, p["solar_nomw"], w.pub, at, "status_change", None)
    # Not in the window, or not served at all.
    _event(db, p["solar_tx"], w.pub, start - dt.timedelta(minutes=1), "status_change", regular)
    _event(db, p["solar_tx"], w.pub, end, "status_change", regular)
    _event(db, p["solar_tx"], w.pub, at, "removed_from_source", regular, public=False)
    _event(db, p["restricted_only"], w.restricted, at, "status_change", regular)
    _event(db, p["staged_only"], w.staged, at, "status_change", regular)
    db.commit()
    return w


@pytest.fixture()
def world(db: Session) -> World:
    return build_world(db)


@pytest.fixture()
def doc(db: Session, world: World) -> dict[str, Any]:
    return compute_snapshot(db, now=world.now)


def _api_total(client: TestClient, query: str, **extra: str) -> int:
    default_limiter.reset()
    parts = urlsplit(query)
    params = {**dict(parse_qsl(parts.query)), **extra, "limit": "1", "include": "count"}
    resp = client.get(parts.path, params=params)
    assert resp.status_code == 200, resp.text
    total: int = resp.json()["meta"]["total"]
    return total


# ------------------------------------------------------------------------- what is counted
def test_the_blocks_partition_the_visible_set_and_hidden_rows_are_never_counted(
    doc: dict[str, Any], world: World
) -> None:
    proposals = doc["metrics"]["proposals"]
    blocks = ("active", "active_not_listed", "withdrawn", "built", "unknown")
    visible = proposals["visible"]["total"]["records"]
    assert visible == sum(proposals[b]["total"]["records"] for b in blocks)
    hidden = {"restricted_only", "staged_only", "future", "unpublished"}
    assert visible == len(world.proposals) - len(hidden)
    # The 7,000 MW each hidden row states is nowhere, and the mixed row adds its served 50 MW.
    assert proposals["visible"]["total"]["capacity_mw"] < 7000
    assert proposals["active"]["total"]["records"] == 11
    assert proposals["active_not_listed"]["total"]["records"] == 2
    assert {k: v["records"] for k, v in proposals["withdrawn"]["by_lifecycle_state"].items()} == {
        "cancelled": 1,
        "withdrawn": 1,
    }
    assert proposals["withdrawn"]["total"]["records"] == 2
    assert proposals["built"]["total"]["records"] == 1
    assert proposals["unknown"]["total"]["records"] == 1


def _slack(doc: dict[str, Any], subject: str, *fields: str) -> int:
    """How far a figure may be from its list query's total: the records counted by a served value
    of one of `fields` that is not the stored one, which the list filters by
    (`visibility.served_not_stored`)."""
    by_field = doc["visibility"]["served_not_stored"][subject]["by_field"]
    return sum(by_field.get(name, 0) for name in fields)


def _filter_names(param: str | list[str]) -> list[str]:
    return [param] if isinstance(param, str) else list(param)


def _buckets(data: dict[str, Any], dimension: str, nested: bool) -> list[tuple[list[str], Any]]:
    """`(keys, value)` for each bucket of one breakdown, outer key first for a two-level one."""
    if not nested:
        return [([key], value) for key, value in data[dimension].items()]
    return [
        ([outer, inner], value) for outer, level in data[dimension].items() for inner, value in level.items()
    ]


def test_each_figure_is_the_total_of_the_public_list_query_it_names(
    client: TestClient, doc: dict[str, Any]
) -> None:
    queries = doc["queries"]
    filters = doc["bucket_filters"]
    checked = 0
    for block in ("visible", "active", "active_not_listed", "withdrawn", "built", "unknown"):
        data = doc["metrics"]["proposals"][block]
        query = queries[f"proposals.{block}"]
        slack = _slack(doc, "proposals", "lifecycle_state")
        assert abs(_api_total(client, query) - data["total"]["records"]) <= slack, block
        for dimension in data:
            param = filters.get(dimension)
            if dimension == "total" or param is None:
                continue
            names = _filter_names(param)
            for keys, tally in _buckets(data, dimension, len(names) > 1):
                if NULL_KEY in keys:
                    continue
                api = _api_total(client, query, **dict(zip(names, keys, strict=True)))
                slack = _slack(doc, "proposals", "lifecycle_state", *names)
                assert abs(api - tally["records"]) <= slack, (block, dimension, keys)
                checked += 1
    for status in ("open", "announced"):
        for view in ("all", "energy"):
            data = doc["metrics"]["opportunities"][status][view]
            query = queries[f"opportunities.{status}.{view}"]
            block_fields = ("status", "relevance") if view == "energy" else ("status",)
            slack = _slack(doc, "opportunities", *block_fields)
            assert abs(_api_total(client, query) - data["records"]) <= slack, (status, view)
            for dimension in ("by_kind", "by_state"):
                (name,) = _filter_names(filters[dimension])
                for (key,), count in _buckets(data, dimension, False):
                    if key == NULL_KEY:
                        continue
                    api = _api_total(client, query, **{name: key})
                    assert abs(api - count) <= _slack(doc, "opportunities", *block_fields, name), (
                        status,
                        view,
                        key,
                    )
                    checked += 1
    assert (
        _api_total(client, queries["opportunities.visible"])
        == doc["metrics"]["opportunities"]["visible"]["records"]
    )
    activity = doc["metrics"]["activity"]
    assert _api_total(client, queries["activity.visible_events"]) == activity["visible_events"]
    assert checked > 50
    # The slack is not a blanket: only the fields the posture names have any, and only by its count.
    assert doc["visibility"]["served_not_stored"] == {
        "opportunities": {"by_field": {"kind": 1}, "records": 1},
        "proposals": {"by_field": {"capacity_mw": 1}, "records": 1},
    }


def test_a_served_value_differs_from_the_list_only_where_the_posture_says(
    client: TestClient, doc: dict[str, Any]
) -> None:
    """The notice whose stored kind came from the off-surface register is a grant as served and an
    RFP to `kind=`: one record each way, which is what `served_not_stored` says."""
    query = doc["queries"]["opportunities.open.all"]
    by_kind = figure(doc, "opportunities.open.all.by_kind")
    assert by_kind["grant"] == _api_total(client, query, kind="grant") + 1
    assert by_kind["rfp"] == _api_total(client, query, kind="rfp") - 1
    assert figure(doc, "opportunities.open.all.records") == _api_total(client, query)


def test_a_states_landing_figures_rebuild_from_the_document(client: TestClient, doc: dict[str, Any]) -> None:
    """The `/texas` landing's three figures, asked of the API as `web/headline.py` and the map's
    notice ask them, and rebuilt from the document alone."""
    state = "US-TX"
    active_states = ",".join(snapshot.ACTIVE_STATES)
    # Active projects: the map's notice, which hides the records no register lists.
    active = _api_total(
        client, "/v1/proposals", jurisdiction=state, lifecycle_state=active_states, listed="true"
    )
    assert figure(doc, f"proposals.active.by_state.{state}.records") == active == 6
    # Planned data centres and other large loads: active states, listed or not.
    loads = _api_total(
        client, "/v1/proposals", jurisdiction=state, lifecycle_state=active_states, kind="load"
    )
    rebuilt = sum(
        figure(doc, f"proposals.{block}.by_state_kind.{state}.load.records", 0)
        for block in ("active", "active_not_listed")
    )
    assert rebuilt == loads == 2
    assert figure(doc, f"proposals.active_not_listed.by_state_kind.{state}.load.demand_mw") == 250.0
    # RFPs and funding calls open now: the energy-relevant ones.
    opened = _api_total(client, "/v1/opportunities", jurisdiction=state, status="open", relevance="energy")
    assert figure(doc, f"opportunities.open.energy.by_state.{state}") == opened == 1


def test_every_block_is_broken_down_by_state(doc: dict[str, Any]) -> None:
    proposals = doc["metrics"]["proposals"]
    for block in ("active", "active_not_listed", "withdrawn", "built", "unknown"):
        by_state = proposals[block]["by_state"]
        us = proposals[block]["by_country"].get("US", {"records": 0})["records"]
        assert sum(t["records"] for t in by_state.values()) == us, block
    assert {k: v["records"] for k, v in proposals["withdrawn"]["by_state"].items()} == {
        "US-CA": 1,
        "US-TX": 1,
    }
    assert {k: v["records"] for k, v in proposals["built"]["by_state"].items()} == {"US-TX": 1}
    for block in ("active", "active_not_listed"):
        nested = proposals[block]["by_state_kind"]
        for state, kinds in nested.items():
            assert sum(t["records"] for t in kinds.values()) == proposals[block]["by_state"][state]["records"]
    opportunities = doc["metrics"]["opportunities"]
    assert opportunities["open"]["all"]["by_state"] == {"US-AZ": 3, "US-TX": 1}
    assert opportunities["open"]["energy"]["by_state"] == {"US-AZ": 2, "US-TX": 1}
    assert opportunities["announced"]["all"]["by_state"] == {"US-AZ": 1}
    assert doc["bucket_filters"]["by_state_kind"] == ["jurisdiction", "kind"]


def test_capacity_is_summed_by_what_the_kind_means(doc: dict[str, Any]) -> None:
    active = doc["metrics"]["proposals"]["active"]
    total = active["total"]
    assert total["records"] == (
        total["capacity_records"]
        + total["demand_records"]
        + total["mw_missing"]
        + total["not_summed_records"]
    )
    by_kind = active["by_kind"]
    assert by_kind["load"]["capacity_mw"] is None and by_kind["load"]["demand_mw"] == 300.0 + 500.0
    assert by_kind["transmission"]["capacity_mw"] is None
    assert by_kind["transmission"]["not_summed_records"] == 1
    assert by_kind["storage"]["storage_mwh"] == 400.0 and by_kind["storage"]["storage_mwh_records"] == 1
    assert total["mw_missing"] == 1  # the contracted solar project with no MW
    # 200 + 100 (wind) + 100 (storage) + 100 (GB) + 100 (MX) + 100 (US) + 50 (the mixed row as served)
    assert total["capacity_mw"] == 750.0
    assert total["demand_mw"] == 800.0
    texas = active["by_state_kind"]["US-TX"]
    assert texas["load"]["demand_mw"] == 500.0 and texas["load"]["capacity_mw"] is None
    assert texas["generation"]["capacity_mw"] == 200.0 + 50.0
    for tally in active["by_iso"].values():
        assert tally["records"] == (
            tally["capacity_records"]
            + tally["demand_records"]
            + tally["mw_missing"]
            + tally["not_summed_records"]
        )


def test_a_value_a_withheld_register_supplied_is_counted_as_served(db: Session, world: World) -> None:
    doc = compute_snapshot(db, now=world.now)
    assert figure(doc, "proposals.active.by_state.US-TX.capacity_mw") == 200.0 + 100.0 + 50.0
    assert doc["visibility"]["served_not_stored"]["proposals"] == {
        "records": 1,
        "by_field": {"capacity_mw": 1},
    }
    # With the off-surface register on the surface, the stored 999 MW is what is served, and the
    # project only that register lists (7,000 MW) is counted too.
    world.staged.publish_state = "public"
    db.commit()
    doc = compute_snapshot(db, now=world.now)
    assert figure(doc, "proposals.active.by_state.US-TX.capacity_mw") == 200.0 + 100.0 + 999.0 + 7000.0
    assert doc["visibility"]["served_not_stored"] == {
        "opportunities": {"records": 0, "by_field": {}},
        "proposals": {"records": 0, "by_field": {}},
    }


def test_country_state_and_operator_buckets(doc: dict[str, Any]) -> None:
    active = doc["metrics"]["proposals"]["active"]
    assert {k: v["records"] for k, v in active["by_country"].items()} == {"GB": 1, "US": 9, "other": 1}
    states = {k: v["records"] for k, v in active["by_state"].items()}
    assert states == {"US-OK": 1, "US-TX": 6, "US-VA": 1, NULL_KEY: 1}
    isos = {k: v["records"] for k, v in active["by_iso"].items()}
    # ERCO is ERCOT's balancing-authority code: one grid, one bucket.
    assert isos["ERCOT"] == 6 and "ERCO" not in isos
    assert isos[NULL_KEY] == 3
    assert active["by_technology"][NULL_KEY]["records"] == 1


def test_opportunities_open_and_announced_all_and_energy(doc: dict[str, Any]) -> None:
    opp = doc["metrics"]["opportunities"]
    assert opp["visible"]["records"] == 7
    assert opp["open"]["all"]["records"] == 5
    assert opp["open"]["energy"]["records"] == 4  # the janitorial tender is not about energy
    assert opp["open"]["all"]["by_country"] == {"GB": 1, "US": 4}
    # The mixed notice is the grant its public register states, not the RFP the store holds.
    assert opp["open"]["energy"]["by_kind"] == {"grant": 2, "rfp": 2}
    assert opp["announced"]["all"]["records"] == opp["announced"]["energy"]["records"] == 1


# -------------------------------------------------------------------------------- activity
def test_activity_counts_genuine_changes_and_says_what_it_left_out(doc: dict[str, Any]) -> None:
    activity = doc["metrics"]["activity"]
    assert activity["rule"] == snapshot.ACTIVITY_RULE == "genuine-change-v2"
    counted = activity["counted"]
    # Six events changed five subjects: the Texas solar project changed twice.
    assert (counted["events"], counted["subjects"]) == (6, 5)
    assert counted["by_event_type"] == {
        "capacity_changed": 1,
        "created": 2,
        "status_change": 2,
        "withdrawn": 1,
    }
    assert counted["subjects_by_event_type"] == {
        "capacity_changed": 1,
        "created": 2,
        "status_change": 1,
        "withdrawn": 1,
    }
    assert counted["by_subject_type"] == {
        "opportunity": {
            "events": 1,
            "subjects": 1,
            "by_event_type": {"created": 1},
            "subjects_by_event_type": {"created": 1},
        },
        "proposal": {
            "events": 5,
            "subjects": 4,
            "by_event_type": {"capacity_changed": 1, "created": 1, "status_change": 2, "withdrawn": 1},
            "subjects_by_event_type": {
                "capacity_changed": 1,
                "created": 1,
                "status_change": 1,
                "withdrawn": 1,
            },
        },
    }
    # The operator's backfill run was an ordinary fetch against a held snapshot: its change counts.
    assert counted["by_run_trigger"] == {"backfill": 1, "schedule": 5}
    # The first run of each register (two), a released hold, and an event no run wrote; the
    # out-of-window, unpublished and hidden-subject events are not visible at all.
    assert activity["left_out"] == {"first_load": 2, "no_source_run": 1, "released_hold": 1}
    assert activity["visible_events"] == counted["events"] + sum(activity["left_out"].values())


def test_a_backfill_labelled_first_run_is_still_a_first_load(db: Session, world: World) -> None:
    """The label is not what makes a run a baseline: a source's first promoted run is one whatever
    an operator called it, and a later one is a comparison whatever they called it."""
    gb_first = db.query(SourceRun).filter(SourceRun.source_id == world.pub_gb.id).one()
    gb_first.trigger = "backfill"
    world.runs["regular"].trigger = "backfill"
    db.commit()
    activity = compute_snapshot(db, now=world.now)["metrics"]["activity"]
    assert activity["left_out"] == {"first_load": 2, "no_source_run": 1, "released_hold": 1}
    assert activity["counted"]["events"] == 6
    assert activity["counted"]["by_run_trigger"] == {"backfill": 6}


def test_the_window_is_the_previous_utc_day() -> None:
    start, end = activity_window(dt.datetime(2026, 10, 24, 5, 58, 3, tzinfo=UTC))
    assert (start, end) == (dt.datetime(2026, 10, 23, tzinfo=UTC), dt.datetime(2026, 10, 24, tzinfo=UTC))
    later = dt.datetime(2026, 10, 24, 23, 59, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert activity_window(later)[1] == dt.datetime(2026, 10, 25, tzinfo=UTC)


def test_a_naive_now_is_refused(db: Session, statements: list[str]) -> None:
    """A naive moment's UTC day depends on the zone it was meant in; reading it as the host's local
    time would file the figures under the wrong day on a host not set to UTC."""
    naive = dt.datetime(2026, 10, 24, 5, 58)  # noqa: DTZ001 -- the naive moment under test
    with pytest.raises(ValueError, match="timezone-aware"):
        activity_window(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_snapshot(db, now=naive)
    assert statements == [], "refused before reading anything"
    aware = compute_snapshot(db, now=naive.replace(tzinfo=UTC))
    assert aware["as_of"] == "2026-10-24" and aware["generated_at"] == "2026-10-24T05:58:00Z"


def test_a_source_with_no_promoted_run_has_only_first_load_events(db: Session, world: World) -> None:
    for run in db.query(SourceRun).filter(SourceRun.source_id == world.pub.id):
        run.status = "partial"
    db.commit()
    activity = compute_snapshot(db, now=world.now)["metrics"]["activity"]
    assert activity["counted"]["events"] == 0
    assert activity["counted"]["subjects"] == 0 and activity["counted"]["by_run_trigger"] == {}
    assert activity["left_out"]["first_load"] == 8


# ------------------------------------------------------------------- provenance and posture
def test_the_sources_and_their_licences_are_named(doc: dict[str, Any], world: World) -> None:
    sources = {s["source_id"]: s for s in doc["sources"]}
    assert set(sources) == {world.pub.id, world.pub_gb.id}
    pub = sources[world.pub.id]
    assert pub["licence_id"] == world.pub.licence_id and pub["reuse_class"] == "open"
    assert pub["counted_events"] == 6 and pub["opportunity_links"] == 6
    assert pub["proposal_links"] == doc["metrics"]["proposals"]["visible"]["total"]["records"] - 1
    assert sources[world.pub_gb.id]["reuse_class"] == "attribution"
    assert sources[world.pub_gb.id]["retrieved_at_max"] is not None
    assert doc["attribution_line"] == f"Sources: {world.pub_gb.name}; {world.pub.name}."


def test_the_visibility_posture_is_recorded(doc: dict[str, Any]) -> None:
    posture = doc["visibility"]
    assert posture["tier"] == "public"
    assert posture["publishable_reuse_classes"] == sorted(PUBLISHABLE_REUSE_CLASSES)
    assert posture["platform_posture"] == snapshot.posture_of(PUBLISHABLE_REUSE_CLASSES)
    assert posture["withheld_sources"] == 2  # the restricted register and the off-surface one
    # The mixed project's capacity and the mixed notice's kind came from the off-surface register.
    assert posture["served_not_stored"] == {
        "opportunities": {"records": 1, "by_field": {"kind": 1}},
        "proposals": {"records": 1, "by_field": {"capacity_mw": 1}},
    }
    assert snapshot.posture_of(("open", "attribution", "noncommercial")) == "noncommercial"


def test_the_document_names_its_day_and_is_deterministic(db: Session, world: World) -> None:
    first = compute_snapshot(db, now=world.now)
    second = compute_snapshot(db, now=world.now)
    assert document_bytes(first) == document_bytes(second)
    assert first["as_of"] == world.now.date().isoformat()
    assert first["schema_version"] == snapshot.SCHEMA_VERSION
    assert first["kind"] == snapshot.DOCUMENT_KIND


#: The tables the computation may read: the public record and its provenance, nothing else.
PUBLIC_TABLES = frozenset(
    {
        "proposal",
        "proposal_source",
        "opportunity",
        "opportunity_source",
        "event",
        "source",
        "source_run",
        "licence",
        "location",
        "organization",
        "organization_alias",
    }
)


def _every_table(db: Session) -> frozenset[str]:
    """Every table the ORM registers and every table this store holds. The private projects' tables
    are registered only by `services.projects`, which this package may not import
    (`infra/importlinter.ini`), so its code cannot name their models either."""
    return frozenset(Base.metadata.tables) | frozenset(sa_inspect(db.get_bind()).get_table_names())


def _tables_named(statement: str, tables: frozenset[str]) -> set[str]:
    """Every table whose name appears in `statement` as a whole identifier, wherever it stands: a
    `FROM`, a `JOIN`, a subquery, a qualified column (`proposal.kind`), a quoted name."""
    return {
        name
        for name in tables
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", statement, re.IGNORECASE)
    }


@pytest.fixture()
def statements(db: Session) -> Iterator[list[str]]:
    seen: list[str] = []
    engine = db.get_bind()

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        sa_event.remove(engine, "before_cursor_execute", _record)


def test_the_computation_reads_only_the_public_record(
    db: Session, world: World, statements: list[str]
) -> None:
    compute_snapshot(db, now=world.now)
    every = _every_table(db)
    # The scan covers the private tables: accounts, alerts, saved searches, sites, sessions, people.
    assert {"user", "account", "alert", "saved_search", "site", "session", "subscription"} <= every
    assert PUBLIC_TABLES <= every
    tables = set().union(*(_tables_named(statement, every) for statement in statements))
    assert tables, "the listener saw the statements"
    assert tables <= PUBLIC_TABLES, sorted(tables - PUBLIC_TABLES)
    assert not any(re.match(r"^\s*(INSERT|UPDATE|DELETE)\b", s, re.IGNORECASE) for s in statements)


def test_the_table_scan_finds_a_table_wherever_a_statement_names_it() -> None:
    every = frozenset({"user", "proposal", "event", "saved_search"})
    assert _tables_named('SELECT 1 WHERE EXISTS (SELECT 1 FROM "user" WHERE x)', every) == {"user"}
    assert _tables_named("SELECT saved_search.id, proposal.kind FROM proposal, saved_search", every) == {
        "proposal",
        "saved_search",
    }
    # A longer identifier is not the table: `event_run` is an alias, `event_type` a column.
    assert _tables_named("SELECT event_run.id, x.event_type FROM source_run AS event_run", every) == set()


def test_the_document_carries_no_record_names_or_people(doc: dict[str, Any], world: World) -> None:
    text = json.dumps(doc)
    for record in [*world.proposals.values(), *world.opportunities.values()]:
        name = record.name_canonical if isinstance(record, Proposal) else record.title
        assert name not in text
        assert record.public_id not in text
