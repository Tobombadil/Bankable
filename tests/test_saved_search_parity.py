"""Saved-search matcher parity: the durable guard that a saved search or webhook alerts on exactly
the records its list endpoint returns for the same query (services/README.md "Pro tier and alerts",
open decision 7).

`services/alerts/matching.py` re-implements the list endpoints' filter grammar in Python, one row
at a time. Until 2026-09-27 it covered a subset, and nothing compared the two, so a saved search
for "storage over 500 MW in US-TX" alerted on every storage proposal anywhere; separately, the list
endpoint allowlisted `state` and never applied it. This test builds a fixture store that covers
every filter dimension (NULL columns, boundary values, restricted-precision locations, slippage
windows, a gated source, a hidden sponsor), then for each query in a generated set -- every filter
alone, and combinations -- asserts at the public and the Pro tier that the ids the list endpoint
returns (paging through all of them) equal the ids the matcher accepts over every visible row.

It fails when either side ignores a filter: parity catches one side ignoring it, and the
"narrows" check catches both ignoring it (each single-filter query must select a non-empty strict
subset of the unfiltered set). `test_every_list_filter_is_exercised` fails when a filter is added
to a list endpoint without a parity query here, which is how a filter lands without matcher support.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.alerts.matching import event_matches_query, opportunity_matches_query, proposal_matches_query
from services.api import records, slippage
from services.api.ratelimit import default_limiter
from services.api.resource_queries import EVENT_FILTERS, synthetic_request
from services.api.visibility import (
    event_visibility_filter,
    opportunity_visibility_filter,
    proposal_visibility_filter,
)
from services.db.models import (
    Event,
    InterconnectionPoint,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)
from services.ids import public_id, slugify
from tests.conftest import login, make_account, make_user

UTC = dt.UTC
#: The slippage clock, pinned on both sides (`records.slip_today`, `slippage.today`).
TODAY = dt.date(2026, 9, 27)
NOW = dt.datetime.now(UTC)
DUE_BOUNDARY = dt.datetime(2026, 11, 1, tzinfo=UTC)

TIERS = ("public", "pro")
PAGE = 40  # small enough that most queries page


# ------------------------------------------------------------------------------------ fixture store
def _licence(db: Session, id_: str, *, reuse_class: str = "open", raw: bool = True) -> Licence:
    lic = Licence(
        id=id_,
        name=id_,
        reuse_class=reuse_class,
        allows_derived_publication=True,
        allows_raw_publication=raw,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    return lic


def _source(db: Session, id_: str, licence: Licence, publish_state: str = "public") -> Source:
    src = Source(
        id=id_,
        name=id_,
        category="generation_queue",
        jurisdiction="US-TX",
        operator="Test Operator",
        url=f"https://example.org/{id_}",
        access="bulk_file",
        cadence="weekly",
        licence_id=licence.id,
        publish_state=publish_state,
        manifest_version="2026-09-12",
        manifest_hash="0" * 64,
    )
    db.add(src)
    return src


def _org(db: Session, name: str, publish_state: str = "public") -> Organization:
    org = Organization(
        public_id=f"org_{slugify(name)}",
        slug=slugify(name),
        name_canonical=name,
        name_normalised=name.lower(),
        type="developer",
        country="US",
        publish_state=publish_state,
    )
    db.add(org)
    return org


def _location(
    db: Session,
    source: Source,
    *,
    precision: str,
    state_code: str | None,
    county_fips: str | None = None,
    country: str = "US",
) -> Location:
    loc = Location(
        kind="point" if precision == "exact" else "county" if county_fips else "state",
        geom=(-97.7, 30.3) if precision != "unknown" else None,
        precision=precision,
        county_name="Travis" if county_fips else None,
        county_fips=county_fips,
        state_code=state_code,
        country=country,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 9, 10, tzinfo=UTC),
        licence_id=source.licence_id,
    )
    db.add(loc)
    return loc


def _link(db: Session, model: Any, fk: str, row: Any, source: Source, record_id: str) -> None:
    db.add(
        model(
            **{fk: row.id},
            source_id=source.id,
            source_record_id=record_id,
            source_url=f"{source.url}#{record_id}",
            retrieved_at=NOW - dt.timedelta(days=1),
            licence_id=source.licence_id,
            raw={"id": record_id},
            first_seen=NOW - dt.timedelta(days=30),
            last_seen=NOW - dt.timedelta(days=1),
        )
    )


def _pick(values: list[Any], i: int, stride: int = 1) -> Any:
    return values[(i * stride) % len(values)]


@pytest.fixture()
def store(db: Session, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(slippage, "today", lambda: TODAY)
    monkeypatch.setattr(records, "slip_today", lambda: TODAY)

    open_lic = _licence(db, "par-open")
    noraw_lic = _licence(db, "par-noraw", reuse_class="attribution", raw=False)
    db.flush()
    src_a = _source(db, "us.par.a", open_lic)
    src_b = _source(db, "us.par.b", noraw_lic)
    src_g = _source(db, "us.par.gated", open_lic, publish_state="api_only")
    acme = _org(db, "Acme Power")
    hidden = _org(db, "Hidden Holdings", publish_state="unpublished")
    db.flush()

    locations: list[Location | None] = [
        None,
        _location(db, src_a, precision="exact", state_code="US-TX", county_fips=None),
        _location(db, src_b, precision="exact", state_code="US-CA", county_fips=None),  # served as region
        _location(db, src_a, precision="county_centroid", state_code="US-TX", county_fips="48453"),
        _location(db, src_a, precision="county_centroid", state_code="US-CA", county_fips="06037"),
        _location(db, src_a, precision="state_centroid", state_code="US-NY"),
        _location(db, src_a, precision="country_centroid", state_code=None, country="GB"),
        _location(db, src_a, precision="unknown", state_code="US-TX"),
        _location(db, src_a, precision="unknown", state_code=None),
    ]
    db.flush()

    def days_ago(n: int) -> dt.date:
        return TODAY - dt.timedelta(days=n)

    kinds = ["storage", "generation", "load"]
    technologies = ["bess_li_ion", "solar_pv", "wind", None]
    lifecycles = ["announced", "filed", "under_construction", "built", "withdrawn", "unknown", "permitted"]
    jurisdictions = ["US-TX", "US-CA", "GB"]
    isos = ["ERCOT", "CAISO", None]
    capacities = [None, 0.0, 49.999, 50.0, 100.0, 499.999, 500.0, 500.001, 1200.0]
    dates = [
        None,
        days_ago(-100),
        days_ago(0),
        days_ago(90),
        days_ago(91),
        days_ago(365),
        days_ago(366),
        days_ago(1095),
        days_ago(1096),
        days_ago(2000),
    ]
    sponsors = [None, acme, hidden]
    source_sets = [[src_a], [src_b], [src_a, src_g], [src_g], [src_a, src_b]]
    # Grid interconnection points (2026-09-28, lane G1): one named by a public register, one by the
    # `api_only` register (visible to Pro, an unknown id to the public tier), and rows at neither.
    points: list[InterconnectionPoint | None] = []
    for key, src in (("a", src_a), ("g", src_g)):
        point = InterconnectionPoint(
            public_id="",
            operator="ERCOT",
            name_display=f"Point {key} 345kV",
            name_key=f"sub:point {key}|345",
            key_rule="test",
            kind="substation",
            source_id=src.id,
            source_url=src.url,
            retrieved_at=NOW - dt.timedelta(days=1),
            licence_id=src.licence_id,
        )
        db.add(point)
        db.flush()
        point.public_id = public_id("poi", point.id)
        points.append(point)
    points.append(None)
    names = ["Alpha Solar", "Bravo Wind", "Charlie Storage", "Delta_Grid", "Echo 100% Farm"]
    storage = [None, 0.0, 99.999, 100.0, 400.0]

    for i in range(140):
        prop = Proposal(
            public_id="",
            slug="",
            kind=_pick(kinds, i),
            name_canonical=f"{_pick(names, i, 2)} {i}",
            jurisdiction=_pick(jurisdictions, i, 5),
            iso=_pick(isos, i, 2),
            lifecycle_state=_pick(lifecycles, i),
            capacity_mw=_pick(capacities, i, 2),
            technology=_pick(technologies, i, 3),
            publish_state="public",
            published_at=NOW - dt.timedelta(days=20),
            # Every seventh is published but not yet public: Pro sees it, public does not.
            public_at=NOW + dt.timedelta(days=5) if i % 7 == 3 else NOW - dt.timedelta(days=1),
            min_reuse_class="attribution",
            source_count=1,
            sponsor_org_id=(s.id if (s := _pick(sponsors, i, 4)) else None),
            location_id=(loc.id if (loc := _pick(locations, i, 5)) else None),
            proposed_online_date=_pick(dates, i),
            storage_mwh=_pick(storage, i, 3),
            interconnection_point_id=(pt.id if (pt := _pick(points, i, 7)) else None),
            first_seen=NOW - dt.timedelta(days=i % 40, hours=1),
            last_changed=NOW - dt.timedelta(minutes=i),
        )
        db.add(prop)
        db.flush()
        prop.public_id = public_id("prop", prop.id)
        prop.slug = f"{slugify(prop.name_canonical)}-{i}"
        for j, src in enumerate(_pick(source_sets, i, 3)):
            _link(db, ProposalSource, "proposal_id", prop, src, f"Q_{i}-{j}" if src is src_g else f"Q{i}-{j}")
    db.flush()

    opp_kinds = ["rfp", "grant", "tender"]
    statuses = ["open", "closed", "awarded", "announced", "open", "cancelled"]
    opp_techs: list[list[str]] = [[], ["solar_pv"], ["wind", "bess_li_ion"], ["bess_li_ion"], ["hydrogen"]]
    opp_jurisdictions = ["US-AZ", "US-TX", "GB"]
    dues = [
        None,
        DUE_BOUNDARY - dt.timedelta(seconds=1),
        DUE_BOUNDARY,
        DUE_BOUNDARY + dt.timedelta(seconds=1),
        DUE_BOUNDARY + dt.timedelta(days=45),
        DUE_BOUNDARY - dt.timedelta(days=45),
    ]
    issuers = [None, acme, hidden]
    titles = ["Solar RFP", "Wind Tender", "Storage Grant", "Grid_Upgrade Call", "Hydro Notice"]
    opens: list[dt.date | None] = [None, dt.date(2026, 1, 1), dt.date(2026, 6, 1), dt.date(2026, 9, 1)]
    sought: list[float | None] = [None, 10.0, 49.999, 50.0, 200.0]
    # Mixed currencies on purpose: `budget_amount[gte]` must never compare across them (records.py
    # `budget_bound`). A CZK amount above every EUR bound, an exact-boundary EUR amount, and an EUR
    # row with no amount.
    budgets: list[tuple[float | None, str | None]] = [
        (None, None),
        (1_000_000.0, "EUR"),
        (999_999.99, "EUR"),
        (25_000_000.0, "CZK"),
        (2_000_000.0, "USD"),
        (None, "EUR"),
        (3_000_000.0, "EUR"),
    ]
    for i in range(80):
        opp = Opportunity(
            public_id="",
            slug="",
            kind=_pick(opp_kinds, i),
            title=f"{_pick(titles, i)} {i}",
            jurisdiction=_pick(opp_jurisdictions, i, 2),
            technologies=_pick(opp_techs, i, 3),
            status=_pick(statuses, i),
            due_at=_pick(dues, i, 5),
            open_at=_pick(opens, i, 3),
            capacity_sought_mw=_pick(sought, i, 7),
            budget_amount=_pick(budgets, i, 5)[0],
            budget_currency=_pick(budgets, i, 5)[1],
            first_seen=NOW - dt.timedelta(days=i % 30, hours=2),
            issuer_org_id=(s.id if (s := _pick(issuers, i)) else None),
            publish_state="public",
            published_at=NOW - dt.timedelta(days=10),
            public_at=NOW + dt.timedelta(days=3) if i % 9 == 4 else NOW - dt.timedelta(hours=1),
            min_reuse_class="attribution",
            source_count=1,
            last_changed=NOW - dt.timedelta(minutes=i),
        )
        db.add(opp)
        db.flush()
        opp.public_id = public_id("opp", opp.id)
        opp.slug = f"{slugify(opp.title)}-{i}"
        for j, src in enumerate(_pick(source_sets, i, 2)):
            _link(db, OpportunitySource, "opportunity_id", opp, src, f"N{i}-{j}")
    db.flush()

    proposals = list(db.scalars(select(Proposal).order_by(Proposal.last_changed.desc())).all())
    opportunities = list(db.scalars(select(Opportunity).order_by(Opportunity.last_changed.desc())).all())
    event_types = ["status_change", "capacity_change", "created", "status_change"]
    # `lifecycleXstate` is there to catch an unescaped `_` in the SQLite arm of `changed_key`
    # (a LIKE wildcard): `changed_key=lifecycle_state` must not select it.
    changed_key_sets: list[list[str]] = [
        ["lifecycle_state"],
        ["capacity_mw", "proposed_online_date"],
        [],
        ["lifecycle_state", "capacity_mw"],
        ["lifecycleXstate"],
    ]
    subjects: list[Proposal | Opportunity] = [*proposals[:24], *opportunities[:12]]
    for i, subject in enumerate(subjects):
        src = _pick([src_a, src_b, src_g], i)
        db.add(
            Event(
                subject_type="proposal" if isinstance(subject, Proposal) else "opportunity",
                subject_id=subject.id,
                event_type=_pick(event_types, i),
                observed_at=NOW - dt.timedelta(days=40 - i),
                source_id=src.id,
                source_url=src.url,
                retrieved_at=NOW - dt.timedelta(days=2),
                licence_id=src.licence_id,
                before={},
                after={},
                changed_keys=_pick(changed_key_sets, i),
                public_at=NOW - dt.timedelta(days=1),
                published_at=NOW - dt.timedelta(days=1),
                idempotency_key=f"par:{i}",
            )
        )
        db.flush()
    db.commit()
    events = list(db.scalars(select(Event).order_by(Event.seq)).all())
    return {
        "proposals": proposals,
        "opportunities": opportunities,
        "events": events,
        "orgs": {"acme": acme.public_id, "hidden": hidden.public_id},
        "points": {"public": points[0].public_id, "gated": points[1].public_id},  # type: ignore[union-attr]
    }


#: An organisation id that names nothing: a taken-down organisation must select exactly what this does.
UNKNOWN_ORG = "org_does-not-exist"
#: A point id that names nothing: a point from a register the tier may not see selects what this does.
UNKNOWN_POINT = "poi_0000000000"


# --------------------------------------------------------------------------------------- queries
def _proposal_queries(store: dict[str, Any]) -> list[dict[str, Any]]:
    slug = store["proposals"][5].slug
    acme, hidden = store["orgs"]["acme"], store["orgs"]["hidden"]
    middle = store["proposals"][len(store["proposals"]) // 2]
    late = store["proposals"][10]
    return [
        {"kind": "storage"},
        {"kind": ["storage", "load"]},
        {"technology": "solar_pv"},
        {"technology": "solar_pv,wind"},
        {"lifecycle_state": "filed,under_construction"},
        {"jurisdiction": "US-TX"},
        {"iso": "ERCOT"},
        {"iso": ["ERCOT", "CAISO"]},
        {"source_id": "us.par.b"},
        {"source_id": "us.par.gated"},
        {"source_id": ["us.par.b", "us.par.gated"]},
        {"capacity_mw[gte]": 500},
        {"capacity_mw[gte]": "0"},
        {"capacity_mw[lte]": 50},
        {"capacity_mw[lte]": 499.999},
        {"capacity_mw[gte]": 50, "capacity_mw[lte]": 500},
        {"slug": slug},
        {"county_fips": "48453"},
        {"county_fips": "48453,06037"},
        {"state": "US-TX"},
        {"state": ["US-TX", "US-NY"]},
        {"state": "US-ZZ"},
        {"placement": "exact"},
        {"placement": "region"},
        {"placement": "none"},
        {"placement": "exact,region,none"},
        {"slipped": "true"},
        {"slipped": False},
        {"slip_bucket": "under_1y"},
        {"slip_bucket": "1_to_3y"},
        {"slip_bucket": "over_3y,under_1y"},
        {"slipped": "true", "slip_bucket": "1_to_3y"},
        {"q": "solar"},
        {"q": "acme"},
        {"q": "hidden"},
        {"q": "q1"},
        {"q": "q_1"},
        {"q": "delta_"},
        {"q": "100%"},
        # Combinations: the brief's own example, then facets across columns, locations and links.
        {"technology": "bess_li_ion", "capacity_mw[gte]": 100, "state": "US-TX"},
        {"kind": "storage", "state": "US-CA", "placement": "region"},
        {"state": "US-TX", "slipped": "true"},
        {"county_fips": "48453", "lifecycle_state": "announced,filed,permitted"},
        {"source_id": "us.par.gated", "state": "US-TX,US-CA"},
        {"jurisdiction": "US-TX,GB", "slip_bucket": "over_3y", "capacity_mw[lte]": 500},
        {"q": "wind", "placement": "none"},
        {"state": "US-TX", "technology": "wind", "capacity_mw[gte]": 0},
        # Lane E15 (2026-09-27): the filters documented and refused until then.
        {"sponsor_id": acme},
        {"sponsor_id": hidden},
        {"sponsor_id": UNKNOWN_ORG},
        {"sponsor_id": [acme, hidden]},
        {"storage_mwh[gte]": 100},
        {"storage_mwh[gte]": 0},
        {"first_seen[from]": middle.first_seen.isoformat()},
        {"first_seen[to]": middle.first_seen.isoformat()},
        {"first_seen[from]": late.first_seen.isoformat(), "first_seen[to]": middle.first_seen.isoformat()},
        {"last_changed[from]": middle.last_changed.isoformat()},
        {"last_changed[to]": middle.last_changed.isoformat()},
        {"updated_since": middle.last_changed.isoformat()},
        {"sponsor_id": acme, "storage_mwh[gte]": 50, "last_changed[to]": middle.last_changed.isoformat()},
        # Lane G1 (2026-09-28): grid interconnection points.
        {"interconnection_point_id": store["points"]["public"]},
        {"interconnection_point_id": store["points"]["gated"]},
        {"interconnection_point_id": [store["points"]["public"], store["points"]["gated"]]},
        {"interconnection_point_id": UNKNOWN_POINT},
        {
            "interconnection_point_id": store["points"]["public"],
            "state": "US-TX",
            "lifecycle_state": "filed,built",
        },
    ]


def _opportunity_queries(store: dict[str, Any]) -> list[dict[str, Any]]:
    slug = store["opportunities"][0].slug
    acme, hidden = store["orgs"]["acme"], store["orgs"]["hidden"]
    middle = store["opportunities"][len(store["opportunities"]) // 2]
    return [
        {"status": "closed"},
        {"status": "open,awarded"},
        {"kind": "grant"},
        {"technologies": "solar_pv"},
        {"technologies": ["wind", "bess_li_ion"]},
        {"jurisdiction": "US-TX,GB"},
        {"source_id": "us.par.b"},
        {"source_id": "us.par.gated"},
        {"due_at[from]": "2026-11-01T00:00:00Z"},
        {"due_at[from]": "2026-11-01T02:00:00+02:00"},
        {"due_at[to]": "2026-11-01T00:00:00Z"},
        {"due_at[to]": "2026-11-01"},
        {"due_at[from]": "2026-10-01T00:00:00Z", "due_at[to]": "2026-11-01T00:00:01Z"},
        {"slug": slug},
        {"q": "solar"},
        {"q": "acme"},
        {"q": "hidden"},
        {"q": "n1"},
        {"q": "grid_"},
        {"status": "open,closed", "technologies": "wind", "jurisdiction": "US-AZ"},
        {"status": "closed,awarded,announced", "due_at[from]": "2026-10-01T00:00:00Z", "kind": "rfp,tender"},
        {"source_id": "us.par.gated,us.par.b", "q": "tender"},
        # Lane E15 (2026-09-27).
        {"issuer_id": acme},
        {"issuer_id": hidden},
        {"issuer_id": UNKNOWN_ORG},
        {"issuer_id": f"{acme},{hidden}"},
        {"open_at[from]": "2026-06-01"},
        {"open_at[to]": "2026-06-01"},
        {"open_at[from]": "2026-01-02", "open_at[to]": "2026-09-01"},
        {"capacity_sought_mw[gte]": 50},
        {"capacity_sought_mw[gte]": "0"},
        {"budget_currency": "EUR"},
        {"budget_currency": ["EUR", "USD"]},
        # `budget_amount[gte]` is only defined with one currency, so it never appears alone.
        {"budget_currency": "EUR", "budget_amount[gte]": 1_000_000},
        {"budget_currency": "CZK", "budget_amount[gte]": 1_000_000},
        {"status": "open,closed,awarded", "budget_currency": "EUR", "budget_amount[gte]": 999_999.99},
        {"first_seen[from]": middle.first_seen.isoformat()},
        {"first_seen[to]": middle.first_seen.isoformat()},
        {"last_changed[from]": middle.last_changed.isoformat()},
        {"last_changed[to]": middle.last_changed.isoformat()},
        {"updated_since": middle.last_changed.isoformat()},
        {"issuer_id": acme, "open_at[from]": "2026-01-01", "status": "open,closed"},
    ]


def _event_queries(store: dict[str, Any]) -> list[dict[str, Any]]:
    proposal = store["proposals"][4]
    opportunity = store["opportunities"][1]
    middle = store["events"][len(store["events"]) // 2]
    early = store["events"][len(store["events"]) // 4]
    return [
        {"subject_type": "opportunity"},
        {"event_type": "created"},
        {"event_type": "created,capacity_change"},
        {"source_id": "us.par.b"},
        {"subject_id": proposal.public_id},
        {"subject_id": opportunity.public_id},
        {"since": str(middle.seq)},
        {"since": (NOW - dt.timedelta(days=30)).isoformat()},
        {"subject_type": "proposal", "event_type": "capacity_change", "since": "3"},
        {"changed_key": "lifecycle_state"},
        {"changed_key": "capacity_mw,proposed_online_date"},
        {"changed_key": ["proposed_online_date", "lifecycleXstate"]},
        {"changed_key": "no_such_key"},
        # Inclusive bounds, set exactly on an event's own instant so the boundary row decides.
        {"observed_at[from]": middle.observed_at.isoformat()},
        {"observed_at[to]": middle.observed_at.isoformat()},
        {
            "observed_at[from]": early.observed_at.isoformat(),
            "observed_at[to]": middle.observed_at.isoformat(),
        },
        {"observed_at[to]": (NOW - dt.timedelta(days=30)).strftime("%Y-%m-%d")},
        {"changed_key": "lifecycle_state", "observed_at[from]": early.observed_at.isoformat()},
    ]


# ---------------------------------------------------------------------------------------- driver
def _list_ids(client: TestClient, path: str, query: dict[str, Any], id_key: str) -> list[str]:
    base = synthetic_request(query, path=path).url.query
    ids: list[str] = []
    cursor: str | None = None
    while True:
        default_limiter.reset()
        url = f"{path}?{base}&limit={PAGE}" + (f"&cursor={cursor}" if cursor else "")
        resp = client.get(url.replace("?&", "?"))
        assert resp.status_code == 200, (path, query, resp.text)
        body = resp.json()
        ids.extend(item[id_key] for item in body["data"])
        cursor = body["page"]["next_cursor"]
        if not cursor:
            return ids


def _check(
    client: TestClient,
    *,
    path: str,
    id_key: str,
    rows: list[Any],
    row_id: Callable[[Any], str],
    matches: Callable[[Any, dict[str, Any]], bool],
    queries: list[dict[str, Any]],
    single_filter_baseline: set[str] | None,
    may_be_empty: tuple[dict[str, Any], ...] = (),
) -> None:
    failures = []
    for query in queries:
        listed = _list_ids(client, path, query, id_key)
        assert len(listed) == len(set(listed)), f"{path} returned a record twice for {query}"
        expected = {row_id(r) for r in rows if matches(r, query)}
        if set(listed) != expected:
            list_only, matcher_only = sorted(set(listed) - expected), sorted(expected - set(listed))
            failures.append(f"{query}: list-only={list_only} matcher-only={matcher_only}")
        if single_filter_baseline is not None and len(query) == 1:
            if not expected and query not in may_be_empty:
                failures.append(f"{query}: selects nothing on the fixture store, so no positive case")
            if expected == single_filter_baseline:
                failures.append(f"{query}: selects every row, so the filter is ignored on both sides")
    assert not failures, "\n".join(failures)


def _login(client: TestClient, db: Session, tier: str) -> None:
    client.cookies.clear()
    if tier == "public":
        return
    account = make_account(db, entitlement=tier, name=f"{tier} parity account")
    user = make_user(db, account, email=f"{tier}-parity@example.com")
    db.commit()
    login(client, db, user)


@pytest.mark.parametrize("tier", TIERS)
def test_proposal_list_and_matcher_agree(
    client: TestClient, db: Session, store: dict[str, Any], tier: str
) -> None:
    _login(client, db, tier)
    visible = list(db.scalars(select(Proposal).where(*proposal_visibility_filter(tier))).all())
    public = {p.public_id for p in db.scalars(select(Proposal).where(*proposal_visibility_filter("public")))}
    # The tiers must differ on this store, or the Pro run proves nothing the public one did not.
    assert (
        public < {p.public_id for p in visible} if tier == "pro" else public == {p.public_id for p in visible}
    )
    _check(
        client,
        path="/v1/proposals",
        id_key="public_id",
        rows=visible,
        row_id=lambda p: p.public_id,
        matches=lambda p, q: proposal_matches_query(p, q, tier),
        queries=_proposal_queries(store),
        single_filter_baseline={p.public_id for p in visible},
        # Deliberately empty: a state no location carries, and a gated source at the public tier
        # (the record is visible through another source, but not *through this one*).
        may_be_empty=(
            {"state": "US-ZZ"},
            {"q": "hidden"},  # an unpublished sponsor's name matches nothing, on every tier
            # A taken-down sponsor's id selects nothing, the same as an id that never existed.
            {"sponsor_id": store["orgs"]["hidden"]},
            {"sponsor_id": UNKNOWN_ORG},
            {"interconnection_point_id": UNKNOWN_POINT},
            *([{"source_id": "us.par.gated"}] if tier == "public" else []),
            # The `api_only` register's point is an unknown id to the public tier.
            *([{"interconnection_point_id": store["points"]["gated"]}] if tier == "public" else []),
        ),
    )


@pytest.mark.parametrize("tier", TIERS)
def test_opportunity_list_and_matcher_agree(
    client: TestClient, db: Session, store: dict[str, Any], tier: str
) -> None:
    _login(client, db, tier)
    visible = list(db.scalars(select(Opportunity).where(*opportunity_visibility_filter(tier))).all())
    # The baseline for "narrows" is the list's default set (status=open), which a query without
    # `status` starts from; `{}` is in the check itself so the default is pinned too.
    default_set = {o.public_id for o in visible if o.status == "open"}
    queries = _opportunity_queries(store)
    _check(
        client,
        path="/v1/opportunities",
        id_key="public_id",
        rows=visible,
        row_id=lambda o: o.public_id,
        matches=lambda o, q: opportunity_matches_query(o, q, tier),
        queries=[{}, *queries],
        single_filter_baseline=None,
    )
    may_be_empty = [
        {"q": "hidden"},
        {"issuer_id": store["orgs"]["hidden"]},
        {"issuer_id": UNKNOWN_ORG},
        *([{"source_id": "us.par.gated"}] if tier == "public" else []),
    ]
    for query in queries:
        if len(query) == 1:
            selected = {o.public_id for o in visible if opportunity_matches_query(o, query, tier)}
            assert selected or query in may_be_empty, f"{query} selects nothing on the fixture store"
            assert selected != default_set, f"{query} does not narrow the default set"


@pytest.mark.parametrize("tier", TIERS)
def test_event_list_and_matcher_agree(
    client: TestClient, db: Session, store: dict[str, Any], tier: str
) -> None:
    from services.ids import public_id as make_public_id

    _login(client, db, tier)
    visible = list(db.scalars(select(Event).where(*event_visibility_filter(tier))).all())
    _check(
        client,
        path="/v1/events",
        id_key="id",
        rows=visible,
        row_id=lambda e: make_public_id("evt", e.id),
        matches=event_matches_query,
        queries=_event_queries(store),
        single_filter_baseline={make_public_id("evt", e.id) for e in visible},
        may_be_empty=({"changed_key": "no_such_key"},),
    )


def test_every_list_filter_is_exercised(store: dict[str, Any]) -> None:
    """A filter added to a list endpoint without a parity query here -- and so, in practice,
    without a matcher arm -- fails this test rather than shipping as an alert that ignores it."""

    def keys(queries: list[dict[str, Any]]) -> set[str]:
        return {k for q in queries for k in q}

    # `SYNC_FILTERS` (`updated_since`) is not a saved-search key, but the matcher reads it the same
    # way for a row stored before that rule, so it is pinned here too.
    assert records.PROPOSAL_FILTERS | records.SYNC_FILTERS | {"q"} <= keys(_proposal_queries(store))
    assert records.OPPORTUNITY_FILTERS | records.SYNC_FILTERS | {"q"} <= keys(_opportunity_queries(store))
    assert EVENT_FILTERS <= keys(_event_queries(store))


def test_the_fixture_store_covers_the_dimensions(store: dict[str, Any]) -> None:
    """The parity result is only as strong as the rows it runs over: pin the edge cases in."""
    proposals = store["proposals"]
    assert any(p.location is None for p in proposals)
    assert any(p.location is not None and p.location.state_code is None for p in proposals)
    assert any(p.capacity_mw is None for p in proposals)
    assert {500.0, 499.999, 500.001} <= {float(p.capacity_mw) for p in proposals if p.capacity_mw is not None}
    slips = {slippage.slip_days(p.lifecycle_state, p.proposed_online_date, on=TODAY) for p in proposals} - {
        None
    }
    assert {91, 365, 366, 1095, 1096} <= slips
    assert any(
        p.proposed_online_date == TODAY - dt.timedelta(days=90)
        and p.lifecycle_state in slippage.SLIP_ACTIVE_STATES
        for p in proposals
    )
    grades = {p.location.precision for p in proposals if p.location is not None}
    assert set(records.PLACEMENT_REGION_PRECISIONS) | {"exact", "unknown"} <= grades
    assert any(o.technologies == [] for o in store["opportunities"])
    assert any(o.due_at is None for o in store["opportunities"])


@pytest.mark.parametrize("tier", TIERS)
def test_a_hidden_organisation_id_selects_what_an_unknown_id_does(
    client: TestClient, db: Session, store: dict[str, Any], tier: str
) -> None:
    """No oracle (docs/21 §8 item 3): the hidden organisation sponsors and issues visible records, so
    an empty page for its id is the filter refusing to see it, not an absence of rows -- and the
    page must be byte-for-byte the page an id that never existed gets, on both sides."""
    _login(client, db, tier)
    hidden = store["orgs"]["hidden"]
    hidden_uuid = db.scalar(select(Organization.id).where(Organization.public_id == hidden))
    assert any(p.sponsor_org_id == hidden_uuid for p in store["proposals"])
    assert any(o.issuer_org_id == hidden_uuid for o in store["opportunities"])
    for path, key, extra in (
        ("/v1/proposals", "sponsor_id", ""),
        ("/v1/opportunities", "issuer_id", "&status=open,closed,awarded,announced,cancelled"),
        ("/v1/proposals/geo", "sponsor_id", "&bbox=-180,-90,180,90&zoom=3"),
        ("/v1/opportunities/geo", "issuer_id", "&bbox=-180,-90,180,90&zoom=3"),
        ("/feeds/proposals.json", "sponsor_id", ""),
        ("/feeds/opportunities.json", "issuer_id", ""),
    ):
        default_limiter.reset()
        seen = client.get(f"{path}?{key}={hidden}{extra}")
        default_limiter.reset()
        unknown = client.get(f"{path}?{key}={UNKNOWN_ORG}{extra}")
        assert seen.status_code == unknown.status_code == 200, (path, seen.text)
        a, b = seen.json(), unknown.json()
        for body in (a, b):
            body.pop("meta", None)  # request-scoped (request id, generated_at)
            for volatile in ("feed_url", "home_page_url"):
                body.pop(volatile, None)
        assert a == b, path
    proposal = next(p for p in store["proposals"] if p.sponsor_org_id == hidden_uuid)
    assert not proposal_matches_query(proposal, {"sponsor_id": hidden}, tier)


def test_budget_bound_is_a_strict_subset_of_its_currency(client: TestClient, store: dict[str, Any]) -> None:
    """`budget_amount[gte]` only exists beside one `budget_currency`, so the "each filter narrows" check
    runs on the pair: non-empty, and strictly inside the currency's own set, which holds a CZK amount
    far above the bound that must not leak in."""
    currency = set(_list_ids(client, "/v1/opportunities", {"budget_currency": "EUR"}, "public_id"))
    bounded = set(
        _list_ids(
            client,
            "/v1/opportunities",
            {"budget_currency": "EUR", "budget_amount[gte]": 1_000_000},
            "public_id",
        )
    )
    assert bounded and bounded < currency
    czk = [o for o in store["opportunities"] if o.budget_currency == "CZK" and o.status == "open"]
    assert czk and not {o.public_id for o in czk} & bounded
