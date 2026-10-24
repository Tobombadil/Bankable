"""`GET /v1/stats/capacity` and the map's capacity split (`services/api/front.py`, `geo.py`; lane front-api).

The contract held here:

- every figure is the total of the list it names: `GET /v1/proposals?<list_query>&include=count` is
  the bucket's `records`, and the recorder's tally over that list's served rows is the bucket's
  tally, MW and MWh included, for every bucket of every query tried;
- generating and storage MW are summed apart from demand MW, a line's MW is never summed, and a
  record that states no MW is counted as missing, never as zero;
- nothing a list would not show is counted (a restricted register's row, a row only a register not
  cleared for the public tier lists, as PJM's, a row not yet public, an unpublished row), and a
  value a withheld register supplied is counted as served, never as stored;
- records no register lists any more are never counted in an active state, whatever the selection (the
  site hides them from every view of active states, so no figure's list may show fewer than it counts),
  and are tallied apart; every list a figure names is one the site's own rule leaves unchanged;
- the map's cluster and region features split their MW by lifecycle state and technology by the same
  rule, and the split agrees with the endpoint;
- the responses validate against `api/openapi.yaml`.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api import capacity_kinds
from services.api.conftest import (
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.api.geo import SPLIT_THRESHOLD, GeoLicence, GeoRow, build_geo_feature_collection
from services.api.org_pipeline import CAPACITY_NOT_SUMMED_KINDS
from services.api.ratelimit import default_limiter
from services.db.models import Licence, Proposal, ProposalSource, Source
from services.trends import snapshot
from services.trends.snapshot import ServedProposal, Tally

UTC = dt.UTC
ACTIVE = "announced,filed,studied,permitted,contracted,under_construction"
SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
TALLY_KEYS = (
    "records",
    "capacity_mw",
    "capacity_records",
    "demand_mw",
    "demand_records",
    "mw_missing",
    "not_summed_records",
    "storage_mwh",
    "storage_mwh_records",
)


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


@dataclass
class World:
    pub: Source
    gb: Source
    restricted: Source
    staged: Source
    proposals: dict[str, Proposal] = field(default_factory=dict)
    n: int = 0

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
        self.n += 1
        src = source or self.pub
        # Travis County at region grade: the view is one region feature on the map.
        loc = make_location(db, src, src.licence, geom=(-97.75, 30.33))
        prop = make_visible_proposal(
            db,
            src,
            public_id_suffix=str(self.n),
            lifecycle_state=state,
            technology=technology or "solar",
            jurisdiction=jurisdiction,
            location=loc,
        )
        prop.kind, prop.technology, prop.iso = kind, technology, iso
        prop.capacity_mw, prop.storage_mwh = capacity, storage_mwh
        db.flush()
        self.proposals[key] = prop
        return prop


def build_world(db: Session) -> World:
    now = dt.datetime.now(UTC)
    open_lic = make_open_licence(db)
    w = World(
        pub=make_public_source(db, open_lic, id_="us.test.queue"),
        gb=make_public_source(db, _licence(db, "attr-lic", "attribution"), id_="gb.test.register"),
        restricted=make_public_source(
            db, _licence(db, "restricted-lic", "restricted"), id_="us.test.restricted"
        ),
        staged=make_public_source(db, open_lic, id_="us.test.pjm_like"),
    )
    w.staged.publish_state = "ingest_only"
    p = w.proposal
    p(db, "solar_tx", capacity=200.0)
    p(db, "wind_ok", technology="wind", state="permitted", jurisdiction="US-OK", iso="SPP", capacity=150.0)
    p(db, "bess_tx", kind="storage", technology="bess_li_ion", state="studied", storage_mwh=400.0)
    p(db, "dc_va", kind="load", technology="load", state="announced", capacity=300.0, jurisdiction="US-VA")
    p(db, "line_tx", kind="transmission", technology=None, state="under_construction", capacity=1500.0)
    p(db, "solar_nomw", capacity=None, state="contracted", iso=None)
    p(db, "dc_nomw", kind="load", technology="load", state="announced", capacity=None)
    p(db, "gb_wind", technology="wind", jurisdiction="GB", iso=None, source=w.gb, capacity=60.5)
    p(db, "mx_solar", jurisdiction="MX", iso=None)
    p(db, "us_only", jurisdiction="US", iso="MISO", capacity=0.001)
    for key, kind, technology, capacity in (
        ("delisted", "generation", "solar", 50.0),
        ("delisted_dc", "load", "load", 250.0),
    ):
        gone = p(db, key, kind=kind, technology=technology, state="announced", capacity=capacity)
        link = db.scalars(select(ProposalSource).where(ProposalSource.proposal_id == gone.id)).one()
        link.gone_at = now - dt.timedelta(days=3)
    p(db, "withdrawn", state="withdrawn", iso="CAISO", jurisdiction="US-CA")
    p(db, "built", state="built", capacity=80.0)
    p(db, "unknown", state="unknown", technology="wind")
    # Never counted.
    p(db, "restricted_only", capacity=7000.0, source=w.restricted)
    p(db, "pjm_like", capacity=7000.0, source=w.staged, iso="PJM", jurisdiction="US-PA")
    p(db, "future", capacity=7000.0).public_at = now + dt.timedelta(days=1)
    p(db, "unpublished", capacity=7000.0).publish_state = "unpublished"
    # Public through the public register; its stored capacity came from the off-surface one: served 50.
    mixed = p(db, "mixed", capacity=999.0)
    link = db.scalars(select(ProposalSource).where(ProposalSource.proposal_id == mixed.id)).one()
    link.normalised = {
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
    db.commit()
    return w


@pytest.fixture()
def world(db: Session) -> World:
    return build_world(db)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return loaded


def assert_valid(spec: dict[str, Any], schema_name: str, instance: object) -> None:
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    schema = spec["components"]["schemas"][schema_name]
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{schema_name}: {e.message} at {list(e.absolute_path)}" for e in errors)


def _get(client: TestClient, path: str, params: Mapping[str, str]) -> dict[str, Any]:
    default_limiter.reset()
    resp = client.get(path, params=dict(params))
    assert resp.status_code == 200, (path, params, resp.text)
    body: dict[str, Any] = resp.json()
    return body


def stats(client: TestClient, params: Mapping[str, str]) -> dict[str, Any]:
    return _get(client, "/v1/stats/capacity", params)


def list_rows(client: TestClient, query: Mapping[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        body = _get(
            client, "/v1/proposals", {**query, "limit": "200", **({"cursor": cursor} if cursor else {})}
        )
        rows.extend(body["data"])
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            return rows


def tally_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The recorder's tally over a list's rows as the list serves them."""
    tally = Tally()
    for r in rows:
        tally.add(
            ServedProposal(
                kind=r["kind"],
                technology=r["technology"],
                lifecycle_state=r["lifecycle_state"],
                capacity_mw=r["capacity_mw"],
                storage_mwh=r["storage_mwh"],
                jurisdiction=r["jurisdiction"],
                iso=r["iso"],
                listed=r["listed"],
            )
        )
    return tally.as_dict()


def _buckets(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = [("total", data["total"])]
    if data["not_listed"] is not None:
        out.append(("not_listed", data["not_listed"]))
    for name in ("by_technology", "by_lifecycle_state", "by_technology_and_lifecycle_state", "by_kind"):
        out.extend((name, entry) for entry in data[name])
    return out


QUERIES: tuple[dict[str, str], ...] = (
    {},
    {"country": "US"},
    {"country": "US", "lifecycle_state": ACTIVE},
    {"country": "US", "lifecycle_state": ACTIVE, "listed": "false"},
    {"lifecycle_state": ACTIVE, "listed": "true"},
    {"lifecycle_state": "withdrawn,built,unknown"},
    {"lifecycle_state": "announced,built"},
    {"country": "US", "listed": "true"},
    {"listed": "false"},
    {"country": "GB"},
    {"country": "US,GB", "kind": "load,storage"},
    {"technology": "solar,wind", "iso": "ERCOT"},
    {"jurisdiction": "US-TX", "lifecycle_state": "filed,announced"},
)


def site_narrows(query: Mapping[str, str]) -> bool:
    """Whether the site's list for `query` would ask the API for listed records only
    (`web/unlisted.py`, "The rule"): no `listed` of its own and only active states, a view that names
    no state being the site's default of the active ones."""
    if (query.get("listed") or "").strip():
        return False
    states = [s for s in (query.get("lifecycle_state") or ACTIVE).split(",") if s]
    return all(s in ACTIVE.split(",") for s in states)


def _sum_of_lists(client: TestClient, queries: list[dict[str, str]]) -> tuple[dict[str, Any], int]:
    rows: list[dict[str, Any]] = []
    count = 0
    for query in queries:
        rows.extend(list_rows(client, query))
        count += _get(client, "/v1/proposals", {**query, "limit": "1", "include": "count"})["meta"]["total"]
    return tally_of(rows), count


def test_every_figure_is_the_total_of_the_list_it_names(client: TestClient, world: World) -> None:
    checked = spanning = 0
    for query in QUERIES:
        data = stats(client, query)["data"]
        assert data["served_not_stored"]["records"] <= 1
        for name, bucket in _buckets(data):
            refs = bucket["list_queries"]
            assert bucket["list_query"] == (refs[0] if len(refs) == 1 else None), (query, name)
            if not refs and name == "not_listed":
                # A selection with no active state: nothing can be tallied apart, and no list asks for it.
                states = query["lifecycle_state"].split(",")
                assert bucket["records"] == 0 and not set(states) & set(ACTIVE.split(","))
                continue
            if not refs:
                assert name in ("by_technology", "by_technology_and_lifecycle_state", "by_kind"), name
                assert bucket.get("technology", "x") is None or bucket.get("kind", "x") is None
                continue
            assert all(ref["path"] == "/v1/proposals" for ref in refs)
            # The site shows what the API lists for each of these: its rule narrows none of them.
            assert not any(site_narrows(ref["query"]) for ref in refs), (query, name, refs)
            expected, count = _sum_of_lists(client, [ref["query"] for ref in refs])
            assert {k: bucket[k] for k in TALLY_KEYS} == expected, (query, name, refs)
            assert count == bucket["records"], (query, name, refs)
            checked += 1
            spanning += len(refs) > 1
        # The selection is every record read: the figures and the ones tallied apart.
        selection = {**data["selection"]["query"], "limit": "1", "include": "count"}
        hidden = data["not_listed"]["records"] if data["not_listed"] is not None else 0
        assert _get(client, "/v1/proposals", selection)["meta"]["total"] == data["total"]["records"] + hidden
    assert checked > 100 and spanning > 10


def test_mw_is_summed_by_what_it_is(client: TestClient, world: World) -> None:
    total = stats(client, {"country": "US", "lifecycle_state": ACTIVE})["data"]["total"]
    # Generation and storage: solar 200 + wind 150 + bess 100 + mixed (served) 50 + 0.001; never 999.
    assert total["capacity_mw"] == pytest.approx(500.001)
    assert total["capacity_records"] == 5
    # Demand: the Virginia data centre (the Texas one states no MW; the delisted one is not listed).
    assert (total["demand_mw"], total["demand_records"]) == (300.0, 1)
    # The line's 1,500 MW is transfer capability: counted, never summed.
    assert total["not_summed_records"] == 1
    # States no MW: the contracted solar project and the Texas data centre.
    assert total["mw_missing"] == 2
    assert total["records"] == 9
    assert (total["storage_mwh"], total["storage_mwh_records"]) == (400.0, 1)


def test_nothing_a_list_would_not_show_is_counted(client: TestClient, world: World) -> None:
    data = stats(client, {})["data"]
    # Sixteen visible records, two of them dropped by every register while still `announced`.
    assert len(list_rows(client, {})) == 16
    assert (data["total"]["records"], data["not_listed"]["records"]) == (14, 2)
    assert data["total"]["capacity_mw"] < 7000
    assert all(entry["technology"] != "x" for entry in data["by_technology"])
    gb = stats(client, {"country": "GB"})["data"]["total"]
    assert (gb["records"], gb["capacity_mw"]) == (1, 60.5)
    assert stats(client, {"country": "US", "iso": "PJM"})["data"]["total"]["records"] == 0


def test_a_withheld_registers_value_is_counted_as_served(client: TestClient, world: World) -> None:
    data = stats(client, {"jurisdiction": "US-TX", "lifecycle_state": "filed"})["data"]
    assert data["served_not_stored"] == {"records": 1, "by_field": {"capacity_mw": 1}}
    solar = next(e for e in data["by_technology"] if e["technology"] == "solar")
    assert solar["capacity_mw"] == pytest.approx(200.0 + 50.0)  # the Texas project and `mixed` as served
    assert 999.0 not in {e["capacity_mw"] for e in data["by_technology"]}


def test_records_no_register_lists_are_never_counted_in_an_active_state(
    client: TestClient, world: World
) -> None:
    active = stats(client, {"country": "US", "lifecycle_state": ACTIVE})["data"]
    assert active["hides_not_listed"] is True
    assert active["total"]["list_query"]["query"]["listed"] == "true"
    assert active["not_listed"]["records"] == 2
    assert active["not_listed"]["list_query"]["query"] == {
        "country": "US",
        "lifecycle_state": ACTIVE,
        "listed": "false",
    }
    assert (active["not_listed"]["capacity_mw"], active["not_listed"]["demand_mw"]) == (50.0, 250.0)
    # Every selection that names no `listed` hides them, the default and a mix of states too: the
    # active part of each figure is listed records only, and the other states are counted whole.
    every = stats(client, {"country": "US"})["data"]
    mixed = stats(client, {"lifecycle_state": "announced,built"})["data"]
    for data in (every, mixed):
        assert data["hides_not_listed"] is True and data["not_listed"]["records"] == 2
        announced = next(e for e in data["by_lifecycle_state"] if e["lifecycle_state"] == "announced")
        assert announced["list_query"]["query"]["listed"] == "true"
        assert [q["query"].get("listed") for q in data["total"]["list_queries"]] == ["true", None]
        built = next(e for e in data["by_lifecycle_state"] if e["lifecycle_state"] == "built")
        assert "listed" not in built["list_query"]["query"]
    assert every["total"]["records"] == active["total"]["records"] + 3  # withdrawn, built, unknown
    # A caller that names `listed` hides nothing; `listed=false` asks for exactly the dropped records.
    dropped = stats(client, {"country": "US", "lifecycle_state": ACTIVE, "listed": "false"})["data"]
    assert dropped["hides_not_listed"] is False and dropped["not_listed"] is None
    assert {k: dropped["total"][k] for k in TALLY_KEYS} == {k: active["not_listed"][k] for k in TALLY_KEYS}
    listed = stats(client, {"country": "US", "listed": "true"})["data"]
    assert listed["total"]["records"] == every["total"]["records"]


def test_a_dropped_record_is_counted_the_same_however_the_figure_is_asked_for(
    client: TestClient, db: Session
) -> None:
    """The review's probe (2026-10-23): two Texas `filed` batteries of 101 and 102 MW, the second
    dropped by its only register. "US filed" was 2 records and 203 MW from the default call and 1 and
    101 MW when asked for by state; its link, on the site, listed one."""
    lic = make_open_licence(db)
    w = World(
        pub=make_public_source(db, lic, id_="us.test.queue"),
        gb=make_public_source(db, lic, id_="gb.test.register"),
        restricted=make_public_source(db, lic, id_="us.test.other"),
        staged=make_public_source(db, lic, id_="us.test.staged"),
    )
    w.proposal(db, "kept", kind="storage", technology="bess_li_ion", capacity=101.0)
    gone = w.proposal(db, "gone", kind="storage", technology="bess_li_ion", capacity=102.0)
    link = db.scalars(select(ProposalSource).where(ProposalSource.proposal_id == gone.id)).one()
    link.gone_at = dt.datetime.now(UTC) - dt.timedelta(days=1)
    db.commit()

    by_default = stats(client, {"country": "US"})["data"]
    by_state = stats(client, {"country": "US", "lifecycle_state": "filed"})["data"]
    filed = next(e for e in by_default["by_lifecycle_state"] if e["lifecycle_state"] == "filed")
    battery = next(e for e in by_default["by_technology"] if e["technology"] == "bess_li_ion")
    crossed = by_default["by_technology_and_lifecycle_state"]
    cross = next(e for e in crossed if e["lifecycle_state"] == "filed")
    for figure in (by_default["total"], by_state["total"], filed, battery, cross):
        assert (figure["records"], figure["capacity_mw"]) == (1, 101.0), figure
    assert filed["list_query"]["query"] == {"country": "US", "lifecycle_state": "filed", "listed": "true"}
    assert by_state["total"]["list_query"] == filed["list_query"]
    for data in (by_default, by_state):
        assert (data["not_listed"]["records"], data["not_listed"]["capacity_mw"]) == (1, 102.0)


def test_buckets_are_ordered_and_a_null_has_no_list(client: TestClient, world: World) -> None:
    data = stats(client, {"country": "US"})["data"]
    technologies = [e["technology"] for e in data["by_technology"]]
    assert technologies[-1] is None  # the line states no technology
    unstated = next(e for e in data["by_technology"] if e["technology"] is None)
    assert unstated["list_query"] is None and unstated["list_queries"] == []
    capacity = [e["capacity_mw"] or 0 for e in data["by_technology"][:-1] if e["capacity_mw"] is not None]
    assert capacity == sorted(capacity, reverse=True)
    states = [e["lifecycle_state"] for e in data["by_lifecycle_state"]]
    order = [
        "announced",
        "filed",
        "studied",
        "permitted",
        "contracted",
        "under_construction",
        "built",
        "withdrawn",
        "unknown",
    ]
    assert states == [s for s in order if s in states]
    cross = data["by_technology_and_lifecycle_state"]
    assert sum(e["records"] for e in cross) == data["total"]["records"]
    assert data["rule"]["demand_kinds"] == ["load"]


def test_meta_gives_the_totals_in_gw(client: TestClient, world: World) -> None:
    body = stats(client, {"country": "US", "lifecycle_state": ACTIVE})
    meta = body["meta"]
    assert (meta["capacity_gw"], meta["demand_gw"], meta["storage_gwh"]) == (0.5, 0.3, 0.4)
    assert body["licence_summary"]["sources"][0]["source_id"] == "us.test.queue"
    assert "us.test.pjm_like" not in {s["source_id"] for s in body["licence_summary"]["sources"]}


def test_page_parameters_and_bad_filters_are_refused(client: TestClient, world: World) -> None:
    for params in ({"limit": "5"}, {"sort": "-capacity_mw"}, {"include": "count"}, {"bbox": "1,2,3,4"}):
        resp = client.get("/v1/stats/capacity", params=params)
        assert resp.status_code == 400 and resp.json()["code"] == "unknown_parameter", params
    assert client.get("/v1/stats/capacity", params={"country": "USA"}).status_code == 400
    assert client.get("/v1/stats/capacity", params={"capacity_mw[gte]": "lots"}).status_code == 400


def test_the_capacity_response_matches_its_schema(
    client: TestClient, world: World, spec: dict[str, Any]
) -> None:
    for query in ({}, {"country": "US", "lifecycle_state": ACTIVE}, {"listed": "true"}, {"country": "ZZ"}):
        assert_valid(spec, "CapacityStatsResponse", stats(client, query))


def test_the_kind_rule_is_one_rule() -> None:
    assert CAPACITY_NOT_SUMMED_KINDS == capacity_kinds.CAPACITY_NOT_SUMMED_KINDS
    assert snapshot.DEMAND_KINDS == capacity_kinds.DEMAND_KINDS
    assert snapshot.NOT_SUMMED_KINDS == capacity_kinds.NEVER_SUMMED_KINDS
    assert [capacity_kinds.summed_as(k) for k in ("generation", None, "load", "transmission")] == [
        "capacity",
        "capacity",
        "demand",
        None,
    ]


# ------------------------------------------------------------------------- the map's capacity split
def test_a_region_feature_splits_its_mw_by_the_rule(
    client: TestClient, world: World, spec: dict[str, Any]
) -> None:
    params = {
        "bbox": "-179,-89,179,89",
        "zoom": "4",
        "country": "US",
        "lifecycle_state": ACTIVE,
        "listed": "true",
    }
    body = _get(client, "/v1/proposals/geo", params)
    assert_valid(spec, "GeoResponse", body)
    regions = [
        f["properties"] for f in body["data"]["features"] if f["properties"]["feature_kind"] == "region"
    ]
    assert len(regions) == 1
    region = regions[0]
    figures = stats(client, {"country": "US", "lifecycle_state": ACTIVE})["data"]
    # Every record of the view is placed in Travis County, so the region holds the whole view.
    assert region["count"] == figures["total"]["records"]
    by_state = {
        e["lifecycle_state"]: e["capacity_mw"] for e in figures["by_lifecycle_state"] if e["capacity_mw"]
    }
    assert region["capacity_mw_by_lifecycle_state"] == pytest.approx(by_state)
    by_tech = {e["technology"]: e["capacity_mw"] for e in figures["by_technology"] if e["capacity_mw"]}
    assert region["capacity_mw_by_technology"] == pytest.approx(by_tech)
    assert region["demand_mw_sum"] == figures["total"]["demand_mw"] == 300.0
    # `capacity_mw_sum` keeps its meaning: every stated MW, the line's and the data centre's too.
    assert region["capacity_mw_sum"] == pytest.approx(500.001 + 300.0 + 1500.0)


def _row(i: int, kind: str, state: str, technology: str | None, mw: str | None) -> GeoRow:
    return GeoRow(
        id=i,
        lifecycle_state=state,
        technology=technology,
        capacity_mw=None if mw is None else Decimal(mw),
        geom=(-97.7 + i * 1e-6, 30.3),
        precision="exact",
        county_fips="48453",
        county_name="Travis",
        state_code="US-TX",
        country="US",
        licence=GeoLicence(True),
        kind=kind,
    )


def test_a_cluster_feature_splits_its_mw_exactly() -> None:
    plan = [
        ("generation", "filed", "solar", "0.1"),
        ("storage", "studied", "bess_li_ion", "0.2"),
        ("load", "announced", "load", "1.5"),
        ("transmission", "filed", None, "500"),
        ("generation", "filed", None, "0.3"),
        ("generation", "built", "wind", None),
    ]
    rows = [_row(i, *plan[i % len(plan)]) for i in range(SPLIT_THRESHOLD + 1)]
    fc = build_geo_feature_collection(
        rows,
        bbox=(-180.0, -90.0, 180.0, 90.0),
        zoom=1,
        records_total=len(rows),
        lifecycle_state_counts={},
        technology_counts={},
    )
    [cluster] = [f["properties"] for f in fc["features"]]
    assert cluster["feature_kind"] == "cluster" and cluster["count"] == SPLIT_THRESHOLD + 1
    n = {k: sum(1 for i in range(len(rows)) if i % len(plan) == k) for k in range(len(plan))}
    assert cluster["capacity_mw_by_lifecycle_state"] == {
        "filed": round(n[0] * 0.1 + n[4] * 0.3, 3),
        "studied": round(n[1] * 0.2, 3),
    }
    assert cluster["capacity_mw_by_technology"] == {
        "bess_li_ion": round(n[1] * 0.2, 3),
        "solar": round(n[0] * 0.1, 3),
    }
    assert cluster["demand_mw_sum"] == round(n[2] * 1.5, 3)
