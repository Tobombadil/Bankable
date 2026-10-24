"""`country=` on the proposal and opportunity lists, the map's geo reads, the lifecycle counts the
map's notice makes and the events list (`services/api/country.py`; owner, 2026-10-23: the US first,
Great Britain, EU and World Bank coverage kept and reachable).

The contract held here: for every combination of `country` with the other filters, a list's
`include=count` total is the number of rows paging through that list returns, and those rows are
exactly the rows of the same list without `country` whose jurisdiction names the country; the map's
totals and the notice's four lifecycle counts agree with the list; the SQL clause and its Python
twin (`country_of`) select the same records for every spelling in the store and a set of awkward
ones; a malformed code is a 400 and a saved search refuses the filter.
"""

from __future__ import annotations

import datetime as dt
import itertools
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.country import COUNTRY_FILTERS, country_clause, country_of, country_values
from services.api.errors import ProblemError
from services.api.pro import validate_saved_search_query
from services.api.ratelimit import default_limiter
from services.api.resource_queries import EXPORT_QUERY_KEYS, validate_export_query
from services.db.models import Event, Licence, Opportunity, Proposal, ProposalSource, Source, SourceRun

UTC = dt.UTC
ACTIVE = "announced,filed,studied,permitted,contracted,under_construction"
WORLD_BBOX = "-179,-89,179,89"


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
    now: dt.datetime
    pub: Source
    gb: Source
    restricted: Source
    staged: Source
    proposals: dict[str, Proposal] = field(default_factory=dict)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    n: int = 0

    def proposal(
        self,
        db: Session,
        key: str,
        jurisdiction: str,
        *,
        kind: str = "generation",
        technology: str = "solar",
        state: str = "filed",
        iso: str | None = "ERCOT",
        source: Source | None = None,
        placed: bool = True,
    ) -> Proposal:
        self.n += 1
        src = source or self.pub
        location = None
        if placed:
            country = (country_of(jurisdiction) or "US") if jurisdiction else "US"
            location = make_location(
                db,
                src,
                src.licence,
                state_code=jurisdiction if "-" in jurisdiction else None,
                country=country,
            )
        prop = make_visible_proposal(
            db,
            src,
            public_id_suffix=str(self.n),
            lifecycle_state=state,
            technology=technology,
            jurisdiction=jurisdiction,
            location=location,
        )
        prop.kind, prop.iso = kind, iso
        db.flush()
        self.proposals[key] = prop
        return prop

    def opportunity(self, db: Session, key: str, jurisdiction: str, **kw: Any) -> Opportunity:
        self.n += 1
        opp = make_visible_opportunity(
            db, self.pub, public_id_suffix=str(self.n), status=kw.pop("status", "open")
        )
        opp.jurisdiction = jurisdiction
        for name, value in kw.items():
            setattr(opp, name, value)
        db.flush()
        self.opportunities[key] = opp
        return opp


def build_world(db: Session) -> World:
    now = dt.datetime.now(UTC)
    open_lic = make_open_licence(db)
    w = World(
        now=now,
        pub=make_public_source(db, open_lic, id_="us.test.queue"),
        gb=make_public_source(db, _licence(db, "attr-lic", "attribution"), id_="gb.test.register"),
        restricted=make_public_source(
            db, _licence(db, "restricted-lic", "restricted"), id_="us.test.restricted"
        ),
        staged=make_public_source(db, open_lic, id_="us.test.pjm_like"),
    )
    w.staged.publish_state = "ingest_only"  # as PJM's rows: never public until a licence exists
    db.flush()
    p = w.proposal
    p(db, "tx_solar", "US-TX")
    p(db, "tx_bess", "US-TX", kind="storage", technology="bess_li_ion", state="studied")
    p(db, "ok_wind", "US-OK", technology="wind", state="permitted", iso="SPP")
    p(db, "va_load", "US-VA", kind="load", technology="load", state="announced", iso="PJM")
    p(db, "us_bare", "US", iso="MISO", placed=False)
    p(db, "ocs_wind", "US-OCS", technology="wind", state="announced", iso=None, placed=False)
    p(db, "lower_ca", "us-ca", state="contracted", iso="CAISO", placed=False)
    p(db, "gb_wind", "GB", technology="wind", iso=None, source=w.gb)
    p(db, "gb_eng", "GB-ENG", kind="storage", technology="bess_li_ion", iso=None, source=w.gb)
    p(db, "mx_solar", "MX", iso=None, placed=False)
    p(db, "us_ca_line", "US+CA", kind="transmission", technology="hvdc", iso=None, placed=False)
    p(db, "withdrawn_ca", "US-CA", state="withdrawn", iso="CAISO")
    p(db, "cancelled_gb", "GB", state="cancelled", source=w.gb, iso=None)
    p(db, "built_tx", "US-TX", state="built")
    p(db, "unknown_tx", "US-TX", state="unknown")
    delisted = p(db, "delisted_tx", "US-TX", state="announced")
    link = db.scalars(select(ProposalSource).where(ProposalSource.proposal_id == delisted.id)).one()
    link.gone_at = now - dt.timedelta(days=3)
    # Never counted on any public surface.
    p(db, "restricted_tx", "US-TX", source=w.restricted)
    p(db, "pjm_like", "US-PA", iso="PJM", source=w.staged)
    p(db, "future_gb", "GB", source=w.gb).public_at = now + dt.timedelta(days=1)
    p(db, "unpublished_us", "US-TX").publish_state = "unpublished"

    o = w.opportunity
    o(db, "az_rfp", "US-AZ", technologies=["solar_pv"])
    o(db, "us_grant", "US", kind="foa", technologies=["wind"])
    o(db, "gb_tender", "GB", kind="tender", technologies=["wind"])
    o(db, "de_tender", "DE", kind="tender", title="Janitorial services for the town hall", technologies=[])
    o(db, "eu_tender", "EU", kind="tender", technologies=["bess_li_ion"])
    o(db, "wb_global", "global", kind="procurement_notice", technologies=["solar_pv"])
    o(db, "wb_kenya", "KE", kind="procurement_notice", technologies=["solar_pv"])
    o(db, "closed_tx", "US-TX", status="closed")
    db.commit()

    # Change events, from a run that is not the source's first, on subjects in several countries.
    first = SourceRun(source_id=w.pub.id, started_at=now - dt.timedelta(days=30), status="ok")
    later = SourceRun(source_id=w.pub.id, started_at=now - dt.timedelta(days=2), status="ok")
    db.add_all([first, later])
    db.flush()
    for key, subjects in (("proposal", w.proposals), ("opportunity", w.opportunities)):
        for i, subject in enumerate(subjects.values()):
            for run in (first, later):
                db.add(
                    Event(
                        subject_type=key,
                        subject_id=subject.id,
                        event_type="status_change" if i % 2 else "created",
                        observed_at=run.started_at + dt.timedelta(minutes=5),
                        published_at=run.started_at + dt.timedelta(minutes=10),
                        public_at=run.started_at + dt.timedelta(minutes=10),
                        source_id=w.pub.id,
                        source_url=w.pub.url,
                        retrieved_at=run.started_at,
                        licence_id=w.pub.licence_id,
                        after={"lifecycle_state": "filed" if key == "proposal" else "open"},
                        changed_keys=["lifecycle_state"],
                        run_id=run.id,
                        idempotency_key=f"test:{uuid.uuid4().hex}",
                    )
                )
                db.flush()
    db.commit()
    return w


@pytest.fixture()
def world(db: Session) -> World:
    return build_world(db)


def _get(client: TestClient, path: str, params: Mapping[str, str]) -> dict[str, Any]:
    default_limiter.reset()
    resp = client.get(path, params=dict(params))
    assert resp.status_code == 200, (path, params, resp.text)
    body: dict[str, Any] = resp.json()
    return body


def count(client: TestClient, path: str, params: Mapping[str, str]) -> int:
    total = _get(client, path, {**params, "limit": "1", "include": "count"})["meta"]["total"]
    return int(total)


def paged(client: TestClient, path: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query = {**params, "limit": "3", **({"cursor": cursor} if cursor else {})}
        body = _get(client, path, query)
        rows.extend(body["data"])
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            return rows


COUNTRIES = (None, "US", "GB", "US,GB", "us", "DE", "EU", "MX", "KE", "FR")


def _wanted(spec: str | None) -> set[str] | None:
    return None if spec is None else {c.strip().upper() for c in spec.split(",")}


PROPOSAL_FILTERS: tuple[dict[str, str], ...] = (
    {},
    {"lifecycle_state": ACTIVE},
    {"lifecycle_state": ACTIVE, "listed": "true"},
    {"lifecycle_state": ACTIVE, "listed": "false"},
    {"lifecycle_state": "withdrawn,cancelled"},
    {"lifecycle_state": "built"},
    {"kind": "storage"},
    {"kind": "generation,load"},
    {"technology": "solar,wind"},
    {"jurisdiction": "US-TX,GB"},
    {"iso": "ERCOT"},
    {"iso": "PJM"},
    {"placement": "none"},
)


def test_a_proposal_country_count_is_its_paged_list_for_every_combination(
    client: TestClient, world: World
) -> None:
    checked = 0
    for filters, country in itertools.product(PROPOSAL_FILTERS, COUNTRIES):
        params = {**filters, **({"country": country} if country else {})}
        rows = paged(client, "/v1/proposals", params)
        assert count(client, "/v1/proposals", params) == len(rows), params
        wanted = _wanted(country)
        unscoped = paged(client, "/v1/proposals", filters)
        expected = {
            r["public_id"] for r in unscoped if wanted is None or country_of(r["jurisdiction"]) in wanted
        }
        assert {r["public_id"] for r in rows} == expected, params
        checked += 1
    assert checked == len(PROPOSAL_FILTERS) * len(COUNTRIES)
    us = {r["jurisdiction"] for r in paged(client, "/v1/proposals", {"country": "US"})}
    assert us == {"US-TX", "US-OK", "US-VA", "US", "US-OCS", "us-ca", "US-CA"}
    gb = {r["public_id"] for r in paged(client, "/v1/proposals", {"country": "GB"})}
    assert gb == {world.proposals[k].public_id for k in ("gb_wind", "gb_eng", "cancelled_gb")}


def test_never_public_rows_are_in_no_country(client: TestClient, world: World) -> None:
    hidden = {
        world.proposals[k].public_id for k in ("restricted_tx", "pjm_like", "future_gb", "unpublished_us")
    }
    for country in ("US", "GB", "US,GB"):
        listed = {r["public_id"] for r in paged(client, "/v1/proposals", {"country": country})}
        assert not hidden & listed


def test_the_countries_partition_the_list(client: TestClient, world: World) -> None:
    rows = paged(client, "/v1/proposals", {})
    by_country = Counter(country_of(r["jurisdiction"]) for r in rows)
    assert by_country == {"US": 11, "GB": 3, "MX": 1, None: 1}
    for code, n in by_country.items():
        if code is not None:
            assert count(client, "/v1/proposals", {"country": code}) == n
    assert sum(by_country.values()) == count(client, "/v1/proposals", {})


def test_the_map_totals_and_the_notice_counts_agree_with_the_list(client: TestClient, world: World) -> None:
    for filters, country in itertools.product(PROPOSAL_FILTERS[:6], ("US", "GB", "US,GB")):
        params = {**filters, "country": country}
        geo = _get(client, "/v1/proposals/geo", {**params, "bbox": WORLD_BBOX, "zoom": "3"})["data"]
        assert geo["totals"]["records"] == count(client, "/v1/proposals", params), params
        assert sum(geo["totals"]["lifecycle_state_counts"].values()) == geo["totals"]["records"]
    # The notice (`web/app.py::lifecycle_counts`): active (listed), withdrawn, built, unknown, each a
    # `limit=1&include=count` read with the view's other filters, here `country=US`.
    buckets = {
        "active": {"lifecycle_state": ACTIVE, "listed": "true"},
        "withdrawn": {"lifecycle_state": "withdrawn,cancelled"},
        "built": {"lifecycle_state": "built"},
        "unknown": {"lifecycle_state": "unknown"},
    }
    counts = {name: count(client, "/v1/proposals", {**q, "country": "US"}) for name, q in buckets.items()}
    assert counts == {"active": 7, "withdrawn": 1, "built": 1, "unknown": 1}
    rows = paged(client, "/v1/proposals", {"country": "US"})
    not_listed = count(
        client, "/v1/proposals", {"country": "US", "lifecycle_state": ACTIVE, "listed": "false"}
    )
    assert sum(counts.values()) + not_listed == len(rows)


OPPORTUNITY_FILTERS: tuple[dict[str, str], ...] = (
    {},
    {"status": "open"},
    {"status": "open", "relevance": "energy"},
    {"status": "open,closed"},
    {"status": "open,closed", "relevance": "all"},
    {"kind": "tender,procurement_notice"},
    {"jurisdiction": "US-AZ,GB"},
)


def test_an_opportunity_country_count_is_its_paged_list_for_every_combination(
    client: TestClient, world: World
) -> None:
    for filters, country in itertools.product(OPPORTUNITY_FILTERS, COUNTRIES):
        params = {**filters, **({"country": country} if country else {})}
        rows = paged(client, "/v1/opportunities", params)
        assert count(client, "/v1/opportunities", params) == len(rows), params
        wanted = _wanted(country)
        unscoped = paged(client, "/v1/opportunities", filters)
        expected = {
            r["public_id"] for r in unscoped if wanted is None or country_of(r["jurisdiction"]) in wanted
        }
        assert {r["public_id"] for r in rows} == expected, params
    # TED's own fallback (`EU`) and a World Bank project country are reachable; `global` is not a country.
    assert count(client, "/v1/opportunities", {"country": "EU"}) == 1
    assert count(client, "/v1/opportunities", {"country": "KE"}) == 1
    everything = paged(client, "/v1/opportunities", {"status": "open,closed"})
    assert sum(1 for r in everything if country_of(r["jurisdiction"]) is None) == 1


def test_the_opportunity_map_counts_by_country(client: TestClient, world: World) -> None:
    geo = _get(client, "/v1/opportunities/geo", {"bbox": WORLD_BBOX, "zoom": "3", "country": "US"})["data"]
    assert geo["totals"]["records"] == count(client, "/v1/opportunities", {"country": "US"}) == 2


EVENT_FILTERS: tuple[dict[str, str], ...] = (
    {},
    {"subject_type": "proposal"},
    {"subject_type": "opportunity"},
    {"event_type": "created"},
    {"genuine": "true"},
    {"genuine": "false", "subject_type": "proposal"},
)


def test_an_event_country_count_is_its_paged_list(client: TestClient, world: World, db: Session) -> None:
    subjects = {
        **{p.id: country_of(p.jurisdiction) for p in world.proposals.values()},
        **{o.id: country_of(o.jurisdiction) for o in world.opportunities.values()},
    }
    by_public_id = {
        **{p.public_id: p.id for p in world.proposals.values()},
        **{o.public_id: o.id for o in world.opportunities.values()},
    }
    for filters, country in itertools.product(EVENT_FILTERS, ("US", "GB", "US,GB", "EU", "KE")):
        params = {**filters, "country": country}
        rows = paged(client, "/v1/events", params)
        assert count(client, "/v1/events", params) == len(rows), params
        wanted = _wanted(country) or set()
        unscoped = paged(client, "/v1/events", filters)
        expected = {r["id"] for r in unscoped if subjects[by_public_id[r["subject_id"]]] in wanted}
        assert {r["id"] for r in rows} == expected, params
    # Not vacuous: the US and GB lists hold events of both subject types.
    assert count(client, "/v1/events", {"country": "US", "subject_type": "opportunity"}) == 6
    assert count(client, "/v1/events", {"country": "GB", "subject_type": "proposal"}) == 6


AWKWARD = (
    "US",
    "US-TX",
    "us-tx",
    "Us",
    "uS-ny",
    "USA",
    "US+CA",
    "global",
    "EU",
    "GB",
    "GB-ENG",
    "U",
    "-US",
    "US-",
    "ÜS",
    "uı",
    "U5",
    " US",
    "DE-BY",
    "",
)


def test_the_python_twin_selects_what_the_sql_selects(db: Session, world: World) -> None:
    src = world.pub
    for i, jurisdiction in enumerate(AWKWARD):
        make_visible_proposal(db, src, public_id_suffix=f"9{i:02d}", jurisdiction=jurisdiction)
    db.commit()
    stored = dict(db.execute(select(Proposal.id, Proposal.jurisdiction)).all())
    codes = {c for c in map(country_of, stored.values()) if c} | {"US", "GB", "EU", "DE", "UI", "SS"}
    for code in sorted(codes):
        sql = set(db.scalars(select(Proposal.id).where(country_clause(Proposal.jurisdiction, [code]))))
        python = {pid for pid, j in stored.items() if country_of(j) == code}
        assert sql == python, code
    assert country_of("us-tx") == "US" and country_of("USA") is None and country_of("uı") is None
    assert country_of(" US") is None and country_of("") is None and country_of(None) is None


@pytest.mark.parametrize("value", ["USA", "U", "1A", "U-", "US,Germany"])
def test_a_malformed_code_is_a_400_naming_country(client: TestClient, world: World, value: str) -> None:
    for path in ("/v1/proposals", "/v1/opportunities", "/v1/events"):
        resp = client.get(path, params={"country": value})
        assert resp.status_code == 400, (path, value)
        assert resp.json()["errors"][0]["field"] == "country"
    geo = client.get("/v1/proposals/geo", params={"country": value, "bbox": WORLD_BBOX, "zoom": "3"})
    assert geo.status_code == 400


def test_an_empty_value_is_no_filter(client: TestClient, world: World) -> None:
    assert count(client, "/v1/proposals", {"country": ""}) == count(client, "/v1/proposals", {})
    assert country_values(" , ", "/x") is None
    assert country_values("gb,US,us", "/x") == ["GB", "US"]


def test_a_saved_search_refuses_country_and_an_export_takes_it(db: Session, world: World) -> None:
    for entity in ("proposal", "opportunity", "event"):
        with pytest.raises(ProblemError) as caught:
            validate_saved_search_query(db, entity, {"country": "US"}, "/v1/saved-searches")
        assert caught.value.code == "unknown_parameter"
        assert COUNTRY_FILTERS <= EXPORT_QUERY_KEYS[entity]
        assert validate_export_query(entity, {"country": "US"}, instance="/v1/exports") == {"country": "US"}
