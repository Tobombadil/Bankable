"""`services/ingest/org_enrich.py` against recorded Wikidata and SEC answers (no network).

`fixtures/org_enrich_recorded.json` holds every exchange one fetch over `SPEC` made on 2026-10-22
(re-recorded at 23:12 UTC when the review's cases joined `SPEC`; the cases already there decided
as before): the Query Service queries and answers verbatim, the two `wbsearchentities` answers as
recorded live that morning, and SEC's `company_tickers_exchange.json` cut to the rows whose name key is one of
`SPEC`'s (all of them, so a key that names two registrants still does) plus the registrants in
`EXTRA_SEC_CIKS`. Re-record with `BANKABLE_DOMAIN=<domain> python -m services.ingest.test_org_enrich
--record <searches.json>` after a change to the queries; a replay that asks anything unrecorded
fails (`RecordedTransport`).

The cases are the ones that decide whether a wrong company is ever attached: an LEI join, an LEI
that Wikidata put on another company's item (Johnson Controls), SEC key matches corroborated by the
CIK's item, a same-named foreign parent (National Grid), an item that carries another CIK (Targa)
or its holding company's (Southwest Gas), a utility subsidiary whose SEC "ticker" is a note
(Entergy Louisiana) or whose Wikidata listing is its parent's or a delisted one (PG&E's utility,
Georgia Power, Southern Company Gas), an exact name in the wrong country (Ørsted from GB evidence),
an alias with no country (Fermi America), a person's name in a company column, and a natural person
who is never looked up. Two headquarters are a building and a square (PG&E, First Hydro).

The audit of every match below 0.95 on the full store (2026-10-22) added the rest: a city's name in
the wrong state (`CITY OF ARLINGTON`, Texas, is not Arlington, Washington) and in the right one
(Austin); project companies named after a city and a farm building (`Calipatria, LLC`, `Bailey
Farm, LLC`); a publisher in London and New York for a Maryland paper mill (`VERSO CORP`); a thin
item dissolved in 1996 (`Viridis`); SEC matches whose name-found item is a retailer or an IRS-import
nonprofit (`THE ANDERSONS INC`, `MDU RESOURCES GROUP INC`); and headquarters that are a "college
town" (Las Vegas), a street (Macerich) or a state plus a city (Procter & Gamble), a dissolved
company's website (Anadarko) and a one-word description (Citizens Energy Group's `article`).

The review of that audit (2026-10-22, a replay of the full store's recording) added: local bodies
of a shared name that Wikidata places in another state (`MONTGOMERY COUNTY GOVERNMENT`, Tennessee,
is not Montgomery County, Maryland's; `St. Elizabeth Medical Center`, New York, is not Kentucky's,
and a disambiguation page shares its name) or nowhere (`Sheridan School Corporation`), and one
that the state settles (`THE MOUNT SINAI MEDICAL CENTER`); Georgia Power by SEC key match, whose
CIK's only main-board row is a note Wikidata does not list; an operating subsidiary whose name has
a legal form its listed parent's lacks (`Phillips 66 Company`); a US item headquartered in Israel
(`ALON USA LP`); a customer login beside a homepage (Xcel Energy); and an item that states its
subsidiary's LEI beside its own (Public Service Enterprise Group).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

import pytest
from sqlalchemy.orm import Session

from pipeline.connectors.http import HttpFailed
from pipeline.normalize import org_key
from services.db.models import Asset, AssetOwner, Licence, Organization, Proposal, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest import org_enrich as oe

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "org_enrich_recorded.json"
NOW = dt.datetime(2026, 10, 22, 4, 0, tzinfo=dt.UTC)
PROFILE = oe.PROFILE_KEY

#: (name, where its evidence is, assets, proposals, row LEI, personal data). `where` is an ISO
#: 3166-2 state (assets' `state_code`, proposals' `jurisdiction`) or, as the GB registers write
#: it, a bare country. Each row's state is the real store's (2026-10-20 build). Targets run largest
#: footprint first, ties by name (`collect_targets`); recorded requests do not depend on the order.
SPEC: tuple[tuple[str, str, int, int, str | None, bool], ...] = (
    ("Vistra Corp", "US-TX", 22, 0, "549300KP43CPCUJOOG15", False),
    ("NRG Energy, Inc.", "US-TX", 20, 1, None, False),
    ("DUKE ENERGY CORP", "US-NC", 20, 0, None, False),
    ("Entergy Corp", "US-LA", 19, 0, None, False),
    ("Entergy Louisiana LLC", "US-LA", 18, 0, None, False),
    ("National Grid", "US-NY", 0, 17, None, False),
    ("Targa Resources", "US-TX", 16, 0, None, False),
    ("Johnson Controls, Inc.", "US-WI", 15, 0, "3LB2NG8VUULSCV2NO430", False),
    ("Southwest Gas Corp", "US-NV", 14, 0, "OQQR1IQTGBKH0UR88X59", False),
    ("Georgia Power Co", "US-GA", 13, 0, "D981X4Z4RWS7PDMJUZ03", False),
    ("PACIFIC GAS AND ELECTRIC COMPANY", "US-CA", 12, 0, "1HNPXZSMMB7HMBMVBS46", False),
    ("Southern Company Gas", "US-GA", 11, 0, "549300ZW53G2TUFW5T39", False),
    ("Invenergy", "US-IL", 0, 10, None, False),
    ("Octopus Energy", "GB", 0, 9, None, False),
    ("Centrica", "GB", 0, 8, None, False),
    ("First Hydro Company", "GB", 0, 7, None, False),
    ("Ørsted", "GB", 0, 6, None, False),
    ("Fermi America", "US-TX", 0, 5, None, False),
    ("Nextera Energy Resources, LLC", "US-FL", 4, 0, None, False),
    ("Pine Gate Renewables", "US-NC", 0, 3, None, False),
    ("Elon Musk", "US-TX", 0, 2, None, False),
    ("Warren Buffett", "US-NE", 1, 0, None, True),
    # The gates and fields the 2026-10-22 audit of the full store's matches added (module docstring).
    ("CITY OF ARLINGTON", "US-TX", 1, 0, None, False),  # a place in Washington
    ("CITY OF AUSTIN", "US-TX", 1, 0, None, False),  # a place in the evidence's state
    ("Calipatria, LLC", "US-CA", 1, 0, None, False),  # a legal entity named after a city
    ("Bailey Farm, LLC", "US-NC", 1, 0, None, False),  # ...after a farm building
    ("VERSO CORP", "US-MD", 1, 0, None, False),  # Verso Books: GB and US
    ("Viridis", "US-CA", 2, 0, None, False),  # a thin item dissolved in 1996
    ("THE ANDERSONS INC", "US-OH", 4, 0, None, False),  # SEC match; a retailer's alias is the key
    ("MDU RESOURCES GROUP INC", "US-ND", 6, 0, None, False),  # SEC match; an IRS-import nonprofit
    ("NV ENERGY", "US-NV", 2, 0, None, False),  # Las Vegas is a "college town"
    ("Macerich", "US-NY", 1, 0, None, False),  # headquarters on a street
    ("THE PROCTER & GAMBLE CO", "US-OH", 4, 0, None, False),  # headquarters: Ohio and Cincinnati
    ("Anadarko Petroleum", "US-WY", 7, 0, None, False),  # dissolved: no website
    ("CITIZENS ENERGY GROUP", "US-IN", 7, 0, None, False),  # description "article"
    # The 2026-10-22 review's replay of the full store's recording (module docstring).
    ("MONTGOMERY COUNTY GOVERNMENT", "US-TN", 1, 0, None, False),  # Maryland's county government
    ("St. Elizabeth Medical Center", "US-NY", 0, 1, None, False),  # Kentucky's; a disambiguation page
    ("Sheridan School Corporation", "US-IN", 1, 0, None, False),  # a DC school with no location
    ("THE MOUNT SINAI MEDICAL CENTER", "US-NY", 1, 0, None, False),  # three hospitals; one in New York
    ("GEORGIA POWER COMPANY", "US-GA", 1, 0, None, False),  # no LEI: SEC key match; its CIK lists GPJA
    ("Phillips 66 Company", "US-TX", 1, 0, None, False),  # the operating subsidiary of Phillips 66
    ("ALON USA LP", "US-TX", 1, 0, None, False),  # headquarters in Israel on a US item
    ("Xcel Energy", "US-MN", 1, 0, None, False),  # a customer login beside the homepage
    ("PUBLIC SERVICE ENTERPRISE GROUP INC", "US-NJ", 1, 0, None, False),  # its item states PSE&G's LEI too
)
#: SEC registrants the fixture keeps although no `SPEC` name has their key: the holding companies
#: whose CIK or symbol a subsidiary's Wikidata item carries (Southwest Gas Holdings, PG&E Corp,
#: Johnson Controls International plc).
EXTRA_SEC_CIKS = frozenset({1692115, 1004980, 833444})


# ------------------------------------------------------------------------------------ store
# Rows of the shapes `collect_targets` reads (services.ingest never imports services.api, its test
# factories included: `infra/importlinter.ini`).
_AT = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def _register(session: Session) -> tuple[Licence, Source]:
    lic = Licence(
        id="open-lic",
        name="Open Licence",
        reuse_class="open",
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=_AT,
        classified_by="legal-compliance",
    )
    src = Source(
        id="us.test.register",
        name="Test Register",
        category="generation_queue",
        jurisdiction="US",
        operator="Test Operator",
        url="https://example.org/register",
        access="bulk_file",
        cadence="weekly",
        licence_id=lic.id,
        publish_state="public",
        manifest_version="2026-09-12",
        manifest_hash="0" * 64,
    )
    session.add_all([lic, src])
    session.flush()
    return lic, src


def _org(session: Session, name: str) -> Organization:
    org = Organization(
        public_id="",
        slug="",
        name_canonical=name,
        name_normalised=name.lower(),
        type="developer",
        country="US",
    )
    session.add(org)
    session.flush()
    org.public_id, org.slug = public_id("org", org.id), slugify(name)
    session.flush()
    return org


def _owned_asset(
    session: Session, lic: Licence, src: Source, org: Organization, n: int, country: str, state: str | None
) -> None:
    asset = Asset(
        public_id="",
        slug="",
        asset_type="power_plant",
        source_asset_id=str(n),
        name=f"Plant {n}",
        status="operating",
        technology="wind",
        technologies={"wind": 100.0},
        capacity_mw=100.0,
        unit_count=1,
        attributes={},
        state_code=state,
        country=country,
        source_id=src.id,
        source_url=src.url,
        retrieved_at=_AT,
        licence_id=lic.id,
    )
    session.add(asset)
    session.flush()
    asset.public_id, asset.slug = public_id("asset", asset.id), f"plant-{n}"
    session.add(
        AssetOwner(
            asset_id=asset.id,
            organization_id=org.id,
            role="owner",
            share_pct=100.0,
            owner_name_raw=org.name_canonical,
            source_id=src.id,
            source_url=src.url,
            retrieved_at=_AT,
            licence_id=lic.id,
        )
    )
    session.flush()


def _sponsored_proposal(session: Session, org: Organization, n: int, jurisdiction: str) -> None:
    prop = Proposal(
        public_id="",
        slug="",
        kind="storage",
        name_canonical=f"Test Storage Project {n}",
        jurisdiction=jurisdiction,
        lifecycle_state="filed",
        capacity_mw=100.0,
        technology="bess_li_ion",
        publish_state="public",
        min_reuse_class="open",
        source_count=1,
        sponsor_org_id=org.id,
    )
    session.add(prop)
    session.flush()
    prop.public_id, prop.slug = public_id("prop", prop.id), f"test-storage-project-{n}"
    session.flush()


def build_store(session: Session) -> dict[str, Organization]:
    """One organisation per `SPEC` row, with assets in its country and state and proposals in its
    jurisdiction (`evidence_countries`, `Target.subdivisions`), plus one organisation with neither,
    which is never a target."""
    lic, src = _register(session)
    orgs: dict[str, Organization] = {}
    n = 0
    for name, where, assets, proposals, lei, personal in SPEC:
        org = _org(session, name)
        org.personal_data = personal
        if lei:
            org.ids = {"lei": lei}
        state = where if "-" in where else None
        for _ in range(assets):
            n += 1
            _owned_asset(session, lic, src, org, n, where[:2], state)
        for _ in range(proposals):
            n += 1
            _sponsored_proposal(session, org, n, where)
        orgs[name] = org
    orgs["Idle Holdings LLC"] = _org(session, "Idle Holdings LLC")
    session.commit()
    return orgs


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def recorded() -> oe.RecordedTransport:
    return oe.RecordedTransport(FIXTURE)


def _fetch(session: Session, transport: oe.Transport, **kwargs: Any) -> oe.FetchResult:
    return oe.fetch_profiles(session, transport, clock=lambda: NOW, **kwargs)


@pytest.fixture()
def fetched(
    session: Session, recorded: oe.RecordedTransport
) -> tuple[dict[str, Organization], oe.FetchResult]:
    orgs = build_store(session)
    return orgs, _fetch(session, recorded)


def _by_name(result: oe.FetchResult) -> dict[str, oe.Decision]:
    return {d.name: d for d in result.decisions}


def _values(d: oe.Decision) -> dict[str, Any]:
    return {name: f["value"] for name, f in d.fields.items()}


# ------------------------------------------------------------------------------------ rules
def test_targets_are_the_organisations_with_a_footprint_and_never_a_person(session: Session) -> None:
    build_store(session)
    targets = oe.collect_targets(session)
    names = [t.name for t in targets]
    largest_first = sorted((row for row in SPEC if not row[5]), key=lambda r: (-(r[2] + r[3]), r[0]))
    assert names == [row[0] for row in largest_first]
    assert "Warren Buffett" not in names and "Idle Holdings LLC" not in names
    by = {t.name: t for t in targets}
    assert by["Octopus Energy"].evidence == frozenset({"GB"})
    assert by["Octopus Energy"].subdivisions == frozenset()  # "GB" alone is not a subdivision
    assert by["CITY OF ARLINGTON"].subdivisions == frozenset({"US-TX"})
    assert by["Vistra Corp"].lei == "549300KP43CPCUJOOG15"


def test_each_rule_decides_as_recorded(fetched: tuple[dict[str, Organization], oe.FetchResult]) -> None:
    _orgs, result = fetched
    got = {d.name: (d.status, d.rule, d.qid) for d in result.decisions}
    assert got == {
        "Vistra Corp": ("matched", "lei", "Q104158381"),
        "NRG Energy, Inc.": ("matched", "sec_cik", "Q6955139"),
        "DUKE ENERGY CORP": ("matched", "sec_cik", "Q1264404"),
        "Entergy Corp": ("matched", "sec_cik", "Q1344587"),
        "Entergy Louisiana LLC": ("matched", "sec_cik", "Q87759318"),
        "National Grid": ("conflict", "sec_cik", None),
        "Targa Resources": ("matched", "sec_cik", None),
        "Johnson Controls, Inc.": ("conflict", "lei", None),
        "Southwest Gas Corp": ("matched", "lei", "Q7571280"),
        "Georgia Power Co": ("matched", "lei", "Q5547666"),
        "PACIFIC GAS AND ELECTRIC COMPANY": ("matched", "lei", "Q1815011"),
        "Southern Company Gas": ("matched", "lei", "Q25796"),
        "Invenergy": ("matched", "name_country", "Q6059803"),
        "Octopus Energy": ("matched", "name_country", "Q63242134"),
        "Centrica": ("matched", "name_country", "Q259013"),
        "First Hydro Company": ("matched", "name_country", "Q107432126"),
        "Ørsted": ("no_match", None, None),
        "Fermi America": ("below_threshold", None, "Q137397138"),
        "Nextera Energy Resources, LLC": ("matched", "name_country", "Q3339191"),
        "Pine Gate Renewables": ("no_match", None, None),
        "Elon Musk": ("no_match", None, None),
        "CITY OF ARLINGTON": ("no_match", None, None),
        "CITY OF AUSTIN": ("matched", "name_country", "Q16559"),
        "Calipatria, LLC": ("no_match", None, None),
        "Bailey Farm, LLC": ("no_match", None, None),
        "VERSO CORP": ("below_threshold", None, "Q4010248"),
        "Viridis": ("below_threshold", None, "Q56422438"),
        "THE ANDERSONS INC": ("matched", "sec_cik", None),
        "MDU RESOURCES GROUP INC": ("matched", "sec_cik", None),
        "NV ENERGY": ("matched", "name_country", "Q1961658"),
        "Macerich": ("matched", "sec_cik", "Q6723256"),
        "THE PROCTER & GAMBLE CO": ("matched", "sec_cik", "Q212405"),
        "Anadarko Petroleum": ("matched", "name_country", "Q483637"),
        "CITIZENS ENERGY GROUP": ("matched", "name_country", "Q139815619"),
        "MONTGOMERY COUNTY GOVERNMENT": ("no_match", None, None),
        "St. Elizabeth Medical Center": ("no_match", None, None),
        "Sheridan School Corporation": ("no_match", None, None),
        "THE MOUNT SINAI MEDICAL CENTER": ("matched", "name_country", "Q1476505"),
        "GEORGIA POWER COMPANY": ("matched", "sec_cik", "Q5547666"),
        "Phillips 66 Company": ("below_threshold", "sec_cik", None),
        "ALON USA LP": ("matched", "name_country", "Q2839213"),
        "Xcel Energy": ("matched", "sec_cik", "Q1486956"),
        "PUBLIC SERVICE ENTERPRISE GROUP INC": ("matched", "sec_cik", "Q930919"),
    }
    assert result.report["matched_by_rule"] == {"lei": 5, "sec_cik": 12, "name_country": 11}
    assert result.report["search"] == {
        "searched": 2,
        "with_candidate": 1,
        "failed": 0,
        "not_run": 0,
        "stopped": None,
    }


def test_an_identifier_match_is_certain_and_a_corroborated_sec_match_nearly_so(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    assert by["Vistra Corp"].confidence == 1.0
    assert by["Vistra Corp"].cik == "0001692819"  # read from the LEI item's P5531
    nrg = by["NRG Energy, Inc."]
    assert nrg.confidence == oe.SEC_CORROBORATED and nrg.cik == "0001013871"
    assert any(r.startswith("corroborated: CIK item Q6955139") for r in nrg.reasons)
    # EDGAR writes "ENTERGY CORP /DE/"; the tag is not part of the name, so the key matches.
    assert by["Entergy Corp"].cik == "0000065984" and by["Entergy Corp"].wikidata_rule == "sec_cik"


def test_a_same_named_foreign_registrant_is_a_conflict_and_nothing_is_attached(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    grid = _by_name(fetched[1])["National Grid"]
    assert grid.status == "conflict" and grid.fields == {} and grid.qid is None
    assert any("country ['GB'] not in evidence ['US']" in r for r in grid.reasons)


def test_an_item_that_carries_another_cik_is_never_attached(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    targa = _by_name(fetched[1])["Targa Resources"]
    assert targa.qid is None and targa.fields == {}
    assert any("!= SEC CIK 0001389170" in r for r in targa.reasons)


def test_the_country_gate_and_the_person_gate_reject_exact_names(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    assert any("not in evidence ['GB']" in r for r in by["Ørsted"].reasons)
    musk = by["Elon Musk"]
    assert musk.status == "no_match" and musk.qid is None
    assert any("instance of human (Q5)" in r for r in musk.reasons)


def test_below_the_threshold_nothing_is_written_and_the_reasons_are_reported(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    result = fetched[1]
    fermi = _by_name(result)["Fermi America"]
    assert fermi.confidence is not None and fermi.confidence < oe.MATCH_THRESHOLD
    assert fermi.fields == {}
    assert "name=alias" in fermi.reasons and "country=unknown" in fermi.reasons
    # Largest footprint first, so an operator reads the ones that matter.
    assert [b["name"] for b in result.report["below_threshold_top"]] == [
        "Fermi America",
        "Viridis",
        "Phillips 66 Company",
        "VERSO CORP",
    ]


def test_a_ticker_needs_listing_evidence(fetched: tuple[dict[str, Organization], oe.FetchResult]) -> None:
    by = _by_name(fetched[1])
    nrg = by["NRG Energy, Inc."].fields["ticker"]
    assert nrg["value"] == {"symbol": "NRG", "exchange": "NYSE"}
    assert nrg["source_id"] == oe.SEC_TICKERS_SOURCE_ID
    assert nrg["source_url"] == "https://www.sec.gov/edgar/browse/?CIK=1013871"
    louisiana = by["Entergy Louisiana LLC"]
    assert "ticker" not in louisiana.fields
    assert any(r.startswith("ticker withheld: SEC lists ELC") for r in louisiana.reasons)
    assert "ticker" not in by["Invenergy"].fields  # a private company


def test_every_field_carries_its_source_url_and_retrieved_date(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    duke = by["DUKE ENERGY CORP"]
    values = _values(duke)
    assert values["website"].startswith("https://") and "duke-energy" in values["website"]
    assert values["headquarters"] == {"city": "Charlotte", "country": "US"}
    assert values["wikipedia"] == "https://en.wikipedia.org/wiki/Duke_Energy"
    assert values["lei"] == "I1BZKREC126H0VB1BL91"
    assert values["description"]
    licences = oe.source_licences()
    for d in fetched[1].decisions:
        for name, f in d.fields.items():
            assert set(f) == {"value", "source_id", "source_url", "retrieved_at", "licence_id"}, name
            assert f["licence_id"] == licences[f["source_id"]]
            assert f["source_url"].startswith(("https://www.wikidata.org/wiki/Q", "https://www.sec.gov/"))
            assert f["retrieved_at"] == oe.iso(NOW)


def test_an_lei_that_wikidata_put_on_another_companys_item_is_a_conflict(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    """GLEIF's JOHNSON CONTROLS, INC. (US) LEI is on the item of Johnson Controls International plc
    (IE): an identifier join still has to agree with the row's own countries."""
    jci = _by_name(fetched[1])["Johnson Controls, Inc."]
    assert jci.status == "conflict" and jci.qid is None and jci.fields == {}
    assert jci.reasons == ["LEI item Q667865 country ['BE', 'IE'] not in evidence ['US']"]


def test_a_wikidata_cik_is_kept_only_when_sec_names_this_company(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    assert by["Vistra Corp"].cik == "0001692819"  # SEC: "Vistra Corp."
    swg = by["Southwest Gas Corp"]  # the item carries its holding company's CIK
    assert swg.cik is None
    assert "CIK 0001692115 not recorded: SEC names it 'Southwest Gas Holdings, Inc.'" in swg.reasons
    assert by["Southern Company Gas"].cik is None  # AGL Resources' CIK, no longer listed
    assert (
        "CIK 0001004155 not recorded: it is not in the SEC ticker file" in by["Southern Company Gas"].reasons
    )


def test_a_us_ticker_needs_sec_and_a_foreign_listing_may_come_from_wikidata(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    withheld = {
        "Southwest Gas Corp": "SWX",  # the holding company's
        "Georgia Power Co": "GAR",  # notes, not stock
        "PACIFIC GAS AND ELECTRIC COMPANY": "PCG",  # PG&E Corp's
        "Southern Company Gas": "GAS",  # AGL Resources', delisted in 2016
    }
    for name, symbol in withheld.items():
        assert "ticker" not in by[name].fields, name
        assert any(r.startswith(f"ticker withheld: Wikidata lists {symbol}") for r in by[name].reasons), name
    centrica = by["Centrica"].fields["ticker"]
    assert centrica["value"] == {"symbol": "CNA", "exchange": "London Stock Exchange"}
    assert centrica["source_id"] == oe.WIKIDATA_SOURCE_ID
    assert centrica["source_url"] == "https://www.wikidata.org/wiki/Q259013#P414"


def test_headquarters_is_a_settlement_not_a_building(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    # P159 is the Pacific Gas & Electric Building; P131 climbs to San Francisco.
    assert _values(by["PACIFIC GAS AND ELECTRIC COMPANY"])["headquarters"] == {
        "city": "San Francisco",
        "country": "US",
    }
    assert _values(by["Octopus Energy"])["headquarters"] == {"city": "London", "country": "GB"}


def test_a_place_must_lie_in_a_state_the_evidence_names(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    """A city's government is recorded as the city; `CITY OF ARLINGTON` owns a Texas landfill."""
    by = _by_name(fetched[1])
    arlington = by["CITY OF ARLINGTON"]
    assert arlington.status == "no_match" and arlington.qid is None and arlington.fields == {}
    assert any("a place in ['US-WA'], evidence in ['US-TX']" in r for r in arlington.reasons)
    austin = by["CITY OF AUSTIN"]
    assert (austin.status, austin.rule, austin.qid) == ("matched", "name_country", "Q16559")


def test_a_legal_entity_is_never_a_place_or_a_building(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    """Project companies are named after towns and farms; the name's legal form says it is neither."""
    by = _by_name(fetched[1])
    for name in ("Calipatria, LLC", "Bailey Farm, LLC"):
        d = by[name]
        assert d.status == "no_match" and d.qid is None and d.fields == {}, name
        assert any("has a legal form (llc) but" in r for r in d.reasons), name


def test_weak_evidence_falls_below_the_threshold(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    verso = by["VERSO CORP"]  # Verso Books, by alias, in London and New York
    assert verso.status == "below_threshold" and verso.fields == {}
    assert "country=also:GB" in verso.reasons
    viridis = by["Viridis"]  # a company of 1994-1996 with nothing to check it against
    assert viridis.status == "below_threshold" and viridis.fields == {}
    assert "dissolved (P576)" in viridis.reasons
    assert "thin item: no description, Wikipedia article or website" in viridis.reasons


def test_after_an_sec_match_a_name_found_item_must_be_that_business(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    """SEC decided which company it is: a retailer whose alias is the one-word key, or a same-named
    nonprofit, is not attached; the SEC match and its ticker stand."""
    by = _by_name(fetched[1])
    andersons = by["THE ANDERSONS INC"]
    assert (andersons.status, andersons.rule, andersons.qid) == ("matched", "sec_cik", None)
    assert any("a one-word key matched only an alias" in r for r in andersons.reasons)
    mdu = by["MDU RESOURCES GROUP INC"]
    assert (mdu.status, mdu.rule) == ("matched", "sec_cik") and mdu.qid != "Q131346579"
    assert any("not a business, and SEC lists registrant" in r for r in mdu.reasons)


def test_headquarters_shows_a_city_only_when_wikidata_names_one(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    # Las Vegas is a "college town", which Wikidata files under neighbourhood; it is still the city.
    assert _values(by["NV ENERGY"])["headquarters"] == {"city": "Las Vegas", "country": "US"}
    # P159 is Wilshire Boulevard, "in" Beverly Hills; a street is not climbed.
    assert _values(by["Macerich"])["headquarters"] == {"city": None, "country": "US"}
    # P159 is Ohio and Cincinnati: one city.
    assert _values(by["THE PROCTER & GAMBLE CO"])["headquarters"] == {"city": "Cincinnati", "country": "US"}
    # P159 is Princeton and Houston, neither marked current: the country only.
    nrg = by["NRG Energy, Inc."]
    assert _values(nrg)["headquarters"] == {"city": None, "country": "US"}
    assert "headquarters: Wikidata states 2 places; city withheld" in nrg.reasons


def test_a_dissolved_companys_website_and_a_one_word_description_are_withheld(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    anadarko = by["Anadarko Petroleum"]
    assert anadarko.status == "matched" and "website" not in anadarko.fields
    assert "website withheld: dissolved (P576)" in anadarko.reasons
    assert _values(anadarko)["wikipedia"] == "https://en.wikipedia.org/wiki/Anadarko_Petroleum"
    citizens = by["CITIZENS ENERGY GROUP"]
    assert citizens.status == "matched" and "description" not in citizens.fields


def test_a_local_body_is_matched_only_where_its_evidence_is(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    """A county government, a hospital or a school district acts where it is, and its name recurs
    state by state. Each of the first three was attached to another state's body before the gate."""
    by = _by_name(fetched[1])
    montgomery = by["MONTGOMERY COUNTY GOVERNMENT"]  # the Bi-County landfill, Tennessee
    assert montgomery.status == "no_match" and montgomery.qid is None and montgomery.fields == {}
    assert montgomery.reasons == ["Q139492924: a local body in ['US-MD'], evidence in ['US-TN']"]
    elizabeth = by["St. Elizabeth Medical Center"]  # a New York proposal
    assert elizabeth.status == "no_match" and elizabeth.fields == {}
    assert elizabeth.reasons == [
        "Q16979734: a local body in ['US-KY'], evidence in ['US-NY']",
        "Q7587853: a Wikimedia disambiguation page: the name is shared",
    ]
    sheridan = by["Sheridan School Corporation"]  # an Indiana school district; the item states no place
    assert sheridan.status == "no_match" and sheridan.fields == {}
    assert sheridan.reasons == [
        "Q7494906: a local body's name, but Wikidata does not say where the item is (evidence in ['US-IN'])"
    ]
    # Three hospitals of the name (Manhattan, Miami Beach, Chicago): the evidence's state settles it.
    sinai = by["THE MOUNT SINAI MEDICAL CENTER"]
    assert (sinai.status, sinai.qid) == ("matched", "Q1476505")
    assert "located=US-NY" in sinai.reasons
    assert _values(sinai)["headquarters"] == {"city": "New York City", "country": "US"}


def test_an_sec_ticker_wikidata_does_not_list_is_withheld(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    """Georgia Power by SEC key match (no LEI): its CIK's only main-board row is GPJA, a junior
    subordinated note; Wikidata lists GAR. Two sources that disagree are not two sources."""
    gp = _by_name(fetched[1])["GEORGIA POWER COMPANY"]
    assert (gp.status, gp.rule, gp.cik, gp.qid) == ("matched", "sec_cik", "0000041091", "Q5547666")
    assert "ticker" not in gp.fields
    assert "ticker withheld: SEC lists GPJA for 0000041091; Wikidata lists GAR" in gp.reasons
    # When the two agree the symbol is SEC's, for the verified CIK.
    assert _values(_by_name(fetched[1])["Xcel Energy"])["ticker"] == {"symbol": "XEL", "exchange": "Nasdaq"}


def test_an_operating_subsidiary_is_not_its_listed_parent(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    """`Phillips 66 Company` keys as `PHILLIPS 66`, the registrant's name, which carries no legal
    form; the form is all that tells the two legal entities apart."""
    p66 = _by_name(fetched[1])["Phillips 66 Company"]
    assert (p66.status, p66.confidence) == ("below_threshold", 0.792)
    assert p66.fields == {} and p66.qid is None
    assert "legal form corporation != none (SEC's name has no legal form)" in p66.reasons


def test_one_website_one_lei_and_a_headquarters_in_the_items_country(
    fetched: tuple[dict[str, Organization], oe.FetchResult],
) -> None:
    by = _by_name(fetched[1])
    # Xcel Energy states its homepage and a customer login on a sub-host of it.
    assert _values(by["Xcel Energy"])["website"] == "https://www.xcelenergy.com/"
    # PSEG's item states its own LEI and PSE&G's, neither preferred: which is which is not on it.
    pseg = by["PUBLIC SERVICE ENTERPRISE GROUP INC"]
    assert "lei" not in pseg.fields
    assert any(r.startswith("lei withheld: Wikidata states 2 LEIs") for r in pseg.reasons)
    # A US item headquartered in Israel: one of the two statements is wrong.
    alon = by["ALON USA LP"]
    assert alon.status == "matched" and "headquarters" not in alon.fields
    assert any(
        r.startswith("headquarters withheld: Hof HaSharon Regional Council (IL)") for r in alon.reasons
    )


def test_a_person_is_never_looked_up(fetched: tuple[dict[str, Organization], oe.FetchResult]) -> None:
    _orgs, result = fetched
    recordings = json.loads(FIXTURE.read_text(encoding="utf-8"))["exchanges"]
    asked = json.dumps([e["params"] for e in recordings], ensure_ascii=False)
    assert "Warren Buffett" not in asked and "Buffett" not in asked
    assert "Warren Buffett" not in {d.name for d in result.decisions}


# ------------------------------------------------------------------------------------ apply
def test_apply_writes_profiles_and_websites_and_is_idempotent(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    orgs, result = fetched
    report = oe.apply_profiles(session, result, now=NOW)
    assert report.written == 28 and report.changed == 28 and report.removed == 0
    assert (
        report.websites_written == 23
    )  # of 28: three SEC matches with no item, a dissolved one, First Hydro
    nrg = session.get(Organization, orgs["NRG Energy, Inc."].id)
    assert nrg is not None and nrg.ids is not None
    # The matched item and CIK are the profile's, with their confidence; `ids` is the row's own.
    assert set(nrg.ids) == {PROFILE}
    match = nrg.ids[PROFILE]["match"]
    assert (match["wikidata_id"], match["cik"], match["confidence"]) == ("Q6955139", "0001013871", 0.99)
    assert nrg.website == nrg.ids["profile"]["fields"]["website"]["value"]
    assert nrg.ids["profile"]["website_written"] == nrg.website
    assert nrg.last_changed is not None and nrg.last_changed.replace(tzinfo=None) == NOW.replace(tzinfo=None)
    # A Wikidata LEI is a profile fact, never written over (or into) the row's own `ids["lei"]`.
    assert "lei" not in nrg.ids and nrg.ids["profile"]["fields"]["lei"]["value"] == "5E2UPK5SW04M13XY7I38"
    vistra = session.get(Organization, orgs["Vistra Corp"].id)
    assert vistra is not None and vistra.ids is not None and vistra.ids["lei"] == "549300KP43CPCUJOOG15"
    # Both sources are registered, so the API can render their provenance.
    assert session.get(Source, oe.WIKIDATA_SOURCE_ID) is not None
    assert session.get(Source, oe.SEC_TICKERS_SOURCE_ID) is not None
    later = NOW + dt.timedelta(days=30)
    again = oe.apply_profiles(session, result, now=later)
    assert again.written == 28 and again.changed == 0 and again.unchanged == 28
    assert again.websites_written == 0  # an unchanged rewrite is not a write
    nrg = session.get(Organization, orgs["NRG Energy, Inc."].id)
    assert nrg is not None and nrg.ids is not None
    assert nrg.ids["profile"]["match"]["matched_at"] == oe.iso(NOW)  # unchanged facts keep their date


def test_a_curated_website_is_never_overwritten(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    orgs, result = fetched
    duke = session.get(Organization, orgs["DUKE ENERGY CORP"].id)
    assert duke is not None
    duke.website = "https://example.org/duke-curated"
    session.commit()
    report = oe.apply_profiles(session, result, now=NOW)
    assert report.websites_kept_curated == 1
    duke = session.get(Organization, orgs["DUKE ENERGY CORP"].id)
    assert duke is not None and duke.ids is not None
    assert duke.website == "https://example.org/duke-curated"
    assert duke.ids["profile"]["website_written"] is None


def test_a_negative_decision_removes_exactly_what_was_written(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    orgs, result = fetched
    oe.apply_profiles(session, result, now=NOW)
    nrg = session.get(Organization, orgs["NRG Energy, Inc."].id)
    assert nrg is not None
    nrg.ids = {**(nrg.ids or {}), "eia_utility_id": "13309"}  # curated, never ours
    session.commit()
    nrg_decision = _by_name(result)["NRG Energy, Inc."]
    withdrawn = oe.FetchResult(
        started_at=NOW,
        finished_at=NOW,
        decisions=[oe.Decision(**{**nrg_decision.to_json(), "status": "ambiguous", "fields": {}})],
        report={},
    )
    report = oe.apply_profiles(session, withdrawn, now=NOW)
    assert report.removed == 1
    nrg = session.get(Organization, orgs["NRG Energy, Inc."].id)
    assert nrg is not None
    assert nrg.ids == {"eia_utility_id": "13309"} and nrg.website is None


def test_a_failed_lookup_keeps_the_previous_profile(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    orgs, result = fetched
    oe.apply_profiles(session, result, now=NOW)
    decision = _by_name(result)["Invenergy"]
    failed = oe.FetchResult(
        started_at=NOW,
        finished_at=NOW,
        decisions=[oe.Decision(**{**decision.to_json(), "status": "lookup_failed", "fields": {}})],
        report={},
    )
    report = oe.apply_profiles(session, failed, now=NOW)
    assert report.kept_on_failure == 1
    inv = session.get(Organization, orgs["Invenergy"].id)
    assert (
        inv is not None and inv.ids is not None and inv.ids["profile"]["match"]["wikidata_id"] == "Q6059803"
    )


def test_a_profile_left_on_a_row_later_marked_personal_is_removed(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    orgs, result = fetched
    oe.apply_profiles(session, result, now=NOW)
    inv = session.get(Organization, orgs["Invenergy"].id)
    assert inv is not None
    inv.personal_data = True
    session.commit()
    report = oe.apply_profiles(session, result, now=NOW)
    assert report.removed_personal == 1
    inv = session.get(Organization, orgs["Invenergy"].id)
    assert inv is not None and inv.ids == {} and inv.website is None


def test_a_scored_match_never_becomes_an_identifier_the_row_holds(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    """A scored SEC key match (0.99) written into `ids["cik"]` would be served as an identifier the
    row holds (the JSON-LD prints `ids` as identifiers), and read back next month as "held by the
    row", a certainty (1.0). Nothing outside the profile is written, so neither can happen."""
    _orgs, result = fetched
    oe.apply_profiles(session, result, now=NOW)
    for org in session.query(Organization):
        assert set(org.ids or {}) - {"lei", PROFILE} == set(), org.name_canonical
    assert {t.name: t for t in oe.collect_targets(session)}["NRG Energy, Inc."].cik is None
    next_month = _by_name(_fetch(session, oe.RecordedTransport(FIXTURE)))["NRG Energy, Inc."]
    assert next_month.confidence == oe.SEC_CORROBORATED


def test_a_renamed_or_merged_row_is_skipped(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    orgs, result = fetched
    duke = session.get(Organization, orgs["DUKE ENERGY CORP"].id)
    invenergy = session.get(Organization, orgs["Invenergy"].id)
    assert duke is not None and invenergy is not None
    duke.name_canonical = "Duke Energy Corporation"
    invenergy.merged_into_id = orgs["NRG Energy, Inc."].id
    session.commit()
    report = oe.apply_profiles(session, result, now=NOW)
    assert report.skipped_renamed == 1 and report.skipped_not_eligible == 1
    assert PROFILE not in (session.get(Organization, orgs["DUKE ENERGY CORP"].id).ids or {})  # type: ignore[union-attr]


def test_a_row_that_is_no_longer_a_target_loses_its_profile_decision_or_not(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    """Merged, unpublished or without a footprint since the fetch: the next apply removes what was
    written, whether or not this month's decisions name the row."""
    orgs, result = fetched
    oe.apply_profiles(session, result, now=NOW)
    duke = session.get(Organization, orgs["DUKE ENERGY CORP"].id)
    invenergy = session.get(Organization, orgs["Invenergy"].id)
    assert duke is not None and invenergy is not None and duke.website and invenergy.website
    duke.merged_into_id = orgs["NRG Energy, Inc."].id
    invenergy.publish_state = "unpublished"
    session.commit()
    later = NOW + dt.timedelta(days=30)
    nothing = oe.FetchResult(started_at=later, finished_at=later, decisions=[], report={})
    report = oe.apply_profiles(session, nothing, now=later)
    assert report.removed_not_target == 2
    for org in (duke, invenergy):
        session.refresh(org)
        assert PROFILE not in (org.ids or {}) and org.website is None, org.name_canonical


def test_a_website_an_operator_cleared_is_not_written_back(
    session: Session, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    """An operator removes the website this module wrote (a wrong one, say): next month's match to
    the same item neither writes it back nor shows it in the profile. A curated value is theirs."""
    orgs, result = fetched
    oe.apply_profiles(session, result, now=NOW)
    nrg = session.get(Organization, orgs["NRG Energy, Inc."].id)
    assert nrg is not None and nrg.website
    cleared = nrg.website
    nrg.website = None
    session.commit()
    later = NOW + dt.timedelta(days=30)
    report = oe.apply_profiles(session, result, now=later)
    assert report.websites_refused == 1 and report.websites_written == 0
    session.refresh(nrg)
    assert nrg.website is None and nrg.ids is not None
    assert "website" not in nrg.ids[PROFILE]["fields"]
    assert nrg.ids[PROFILE]["website_refused"] == {"value": cleared, "wikidata_id": "Q6955139"}
    again = oe.apply_profiles(session, result, now=later + dt.timedelta(days=30))
    session.refresh(nrg)
    assert again.websites_refused == 1 and nrg.website is None  # the refusal holds
    nrg.website = "https://example.org/nrg-curated"
    session.commit()
    oe.apply_profiles(session, result, now=later + dt.timedelta(days=60))
    session.refresh(nrg)
    assert nrg.website == "https://example.org/nrg-curated"


def test_decisions_survive_the_file_between_the_two_jobs(
    tmp_path: pathlib.Path, fetched: tuple[dict[str, Organization], oe.FetchResult]
) -> None:
    result = fetched[1]
    path = tmp_path / "decisions.json"
    oe.write_decisions(result, path)
    back = oe.read_decisions(path)
    assert [d.to_json() for d in back.decisions] == [d.to_json() for d in result.decisions]
    assert back.started_at == result.started_at and back.report == json.loads(json.dumps(result.report))


# --------------------------------------------------------------------------------- transport
def test_an_unrecorded_request_fails_instead_of_reaching_the_network(recorded: oe.RecordedTransport) -> None:
    with pytest.raises(HttpFailed, match="not recorded"):
        recorded.get_json(oe.WIKIDATA_SPARQL_URL, {"query": "SELECT * WHERE { ?s ?p ?o }", "format": "json"})


class _Failing:
    """The recorded SEC file; an empty answer to every query; a failure for every search."""

    def __init__(self, recorded: oe.RecordedTransport) -> None:
        self.recorded = recorded
        self.searches = 0

    def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        if url == oe.WIKIDATA_API_URL:
            self.searches += 1
            raise HttpFailed("HTTP 429")
        if url == oe.WIKIDATA_SPARQL_URL:
            return {"results": {"bindings": []}}
        return self.recorded.get_json(url, params)


SEC_KEYED = {"Vistra Corp", "NRG Energy, Inc.", "DUKE ENERGY CORP", "Entergy Corp", "Entergy Louisiana LLC"}
SEC_KEYED |= {"National Grid", "Targa Resources", "Georgia Power Co"}
SEC_KEYED |= {"THE ANDERSONS INC", "MDU RESOURCES GROUP INC", "Macerich", "THE PROCTER & GAMBLE CO"}
SEC_KEYED |= {
    "GEORGIA POWER COMPANY",
    "Phillips 66 Company",
    "Xcel Energy",
    "PUBLIC SERVICE ENTERPRISE GROUP INC",
}


def test_the_search_fallback_stops_after_consecutive_failures_and_keeps_profiles(
    session: Session, recorded: oe.RecordedTransport
) -> None:
    """A shared egress over the Action API's per-minute limit answers 429 to everything: the run
    stops searching after `SEARCH_FAILURE_LIMIT` failures, and every organisation it did not reach
    is `lookup_failed` (its previous profile is kept), while the SEC key matches still decide."""
    build_store(session)
    transport = _Failing(recorded)
    result = _fetch(session, transport)
    by = _by_name(result)
    assert transport.searches == oe.SEARCH_FAILURE_LIMIT
    assert result.report["search"] == {
        "searched": 0,
        "with_candidate": 0,
        "failed": oe.SEARCH_FAILURE_LIMIT,
        "not_run": len(by),  # no Wikidata item for anyone, so every target needed a search
        "stopped": "failures",
    }
    assert {n for n, d in by.items() if d.status == "lookup_failed"} == set(by) - SEC_KEYED
    assert by["NRG Energy, Inc."].status == "matched" and by["NRG Energy, Inc."].qid is None
    assert by["National Grid"].status == "matched"  # no CIK item to contradict it, and no fields
    assert by["National Grid"].fields == {}


def test_a_zero_budget_runs_no_search(session: Session, recorded: oe.RecordedTransport) -> None:
    build_store(session)
    transport = _Failing(recorded)
    result = _fetch(session, transport, search_budget=0)
    assert transport.searches == 0 and result.report["search"]["searched"] == 0
    assert result.report["search"]["stopped"] == "budget"
    assert {d.name for d in result.decisions if d.status == "lookup_failed"} == {
        name for name, *_rest in SPEC if not _rest[-1]
    } - SEC_KEYED


def test_the_search_fallback_ends_when_its_time_is_up(
    session: Session, recorded: oe.RecordedTransport
) -> None:
    """A host that answers slowly but answers (long `Retry-After`s) is waited out until
    `search_minutes`, then the run moves on: the monthly job always ends."""
    build_store(session)
    ticks = iter([0.0, 0.0, 61.0])
    transport = _Failing(recorded)
    result = _fetch(session, transport, search_minutes=1.0, monotonic=lambda: next(ticks, 61.0))
    assert transport.searches == 1  # the one that started before the deadline
    assert result.report["search"]["stopped"] == "time"


def test_user_agents_name_a_real_contact_and_never_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BANKABLE_DOMAIN", raising=False)
    monkeypatch.delenv("PRODUCT_CONTACT_EMAIL", raising=False)
    with pytest.raises(ValueError, match="BANKABLE_DOMAIN"):
        oe.contact()
    domain, email = oe.contact("inframation.io")
    assert email == "hello@inframation.io"
    ua = oe.wikimedia_user_agent(domain, email)
    assert ua.startswith("InframationBot/0.2 (https://inframation.io/bot; hello@inframation.io)")
    assert "Mozilla" not in ua and "Chrome" not in ua
    assert oe.sec_user_agent(domain, email) == "Inframation hello@inframation.io"


def test_a_query_is_followed_by_a_pause_at_least_as_long_as_it_ran() -> None:
    sleeps: list[float] = []
    ticks = iter([10.0, 11.5])

    class _Resp:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}

        def json(self) -> Any:
            return {"results": {"bindings": []}}

    live = oe.LiveTransport(domain="inframation.io", sleep=sleeps.append, clock=lambda: next(ticks))
    sent: list[tuple[str, dict[str, str]]] = []

    def post(url: str, **kwargs: Any) -> _Resp:
        sent.append((url, kwargs["data"]))
        return _Resp()

    live.sparql.post = post  # type: ignore[method-assign,assignment]
    assert live.get_json(oe.WIKIDATA_SPARQL_URL, {"query": "ASK {}", "format": "json"}) == {
        "results": {"bindings": []}
    }
    assert sent == [(oe.WIKIDATA_SPARQL_URL, {"query": "ASK {}", "format": "json"})]
    assert sleeps == [1.5]
    assert live.sparql.session.headers["User-Agent"].startswith("InframationBot/")


# ------------------------------------------------------------------------------------ names
@pytest.mark.parametrize(
    ("name", "core"),
    [
        ("ENTERGY CORP /DE/", "ENTERGY CORP"),
        ("X INC /NEW/ /DE/", "X INC"),
        ("BP PLC /ADR", "BP PLC"),
        ("DOMINION ENERGY, INC", "DOMINION ENERGY, INC"),
    ],
)
def test_edgar_tags_are_not_part_of_the_name(name: str, core: str) -> None:
    assert oe.sec_core_name(name) == core


def test_name_variants_recase_a_name_written_in_capitals() -> None:
    assert oe.name_variants("DUKE ENERGY CORP") == [
        "DUKE ENERGY CORP",
        "DUKE ENERGY",
        "Duke Energy Corp",
        "Duke Energy",
    ]
    assert "CPN Management" in oe.name_variants("CPN MANAGEMENT LP")
    assert "Bank of America" in oe.name_variants("BANK OF AMERICA CORP")
    assert oe.name_variants("NRG Energy, Inc.") == ["NRG Energy, Inc.", "NRG Energy"]


@pytest.mark.parametrize(
    ("name", "form"),
    [
        ("Marathon Petroleum Company LP", "lp"),
        ("Marathon Petroleum Corp", "corporation"),
        ("Holdings A, L.P.", "lp"),
        ("Acme L.L.C.", "llc"),
        ("Invenergy", None),
    ],
)
def test_legal_form_families(name: str, form: str | None) -> None:
    assert oe.legal_form(name) == form


def _item(*classes: str) -> dict[str, Any]:
    return {"claims": {"P31": [{"rank": "normal", "value": c, "q": {}} for c in classes]}}


def test_a_legal_entity_needs_an_organisation_class_that_is_not_physical() -> None:
    """Per P31 class, so Wikidata's tangled tree does not decide by accident: an oil company's class
    that also reaches "building" is still a business; a farm that is also a building, a hotel, and a
    city are not legal entities."""
    org, business, building, structure, facility = "Q43229", "Q4830453", "Q41176", "Q811979", "Q13226383"
    aux = oe._Aux(
        oe.WikidataClient(oe.RecordedTransport({})),
        ancestors={
            "business": {org, business},
            "oil company": {org, business, building, structure},
            "building": {building, structure},
            "farm": {org, business},
            "hotel": {org, business, facility, building},
            "city": {"Q486972", "Q15284", "Q56061", org, "Q783794"},
            "organization": {org},
        },
    )
    assert aux.legal_entity_fit(_item("business", "oil company")) is None
    assert aux.legal_entity_fit(_item("organization")) is None
    for classes in (("farm", "building"), ("hotel",), ("city",)):
        assert aux.legal_entity_fit(_item(*classes)) is not None, classes


def test_a_city_class_is_judged_on_its_own() -> None:
    """Las Vegas is a "college town" (which reaches neighbourhood) and a "big city"; a county of
    Nevada reaches municipality and county; a neighbourhood reaches settlement and neighbourhood."""
    settlement, neighbourhood, county = "Q486972", oe.NEIGHBOURHOOD_CLASS, oe.COUNTY_CLASS
    aux = oe._Aux(
        oe.WikidataClient(oe.RecordedTransport({})),
        ancestors={
            "college town": {settlement, neighbourhood},
            "big city": {settlement, "Q515"},
            "county of Nevada": {"Q15284", county},
            "neighbourhood": {settlement, neighbourhood},
        },
    )
    assert aux.is_city(_item("college town", "big city"))
    assert not aux.is_city(_item("college town"))
    assert not aux.is_city(_item("county of Nevada"))
    assert not aux.is_city(_item("neighbourhood"))


def _entity(
    qid: str, label: str, *, p31: str, aliases: tuple[str, ...] = (), **claims: str
) -> dict[str, Any]:
    entity = oe.empty_entity(qid)
    entity["labels"] = {"en": label}
    entity["aliases"] = {"en": list(aliases)} if aliases else {}
    entity["description"] = f"{label}, described"
    entity["enwiki"] = f"https://en.wikipedia.org/wiki/{qid}"
    entity["claims"] = {
        prop: [{"rank": "normal", "value": value, "q": {}}] for prop, value in {"P31": p31, **claims}.items()
    }
    return entity


@pytest.mark.parametrize(
    ("name", "subdivisions", "candidates", "status", "qid"),
    [
        # "Syracuse, LLC" (a New York storage project) is named after the city, not the university.
        ("Syracuse, LLC", {"US-NY"}, ["Qcity_ny", "Quniversity"], "ambiguous", None),
        ("Syracuse, LLC", {"US-NY"}, ["Quniversity"], "matched", "Quniversity"),
        # A Maine hydro owner "ARCO" and a same-named town in another state: the name is a toponym.
        ("ARCO", {"US-ME"}, ["Qcity_id", "Qoilco"], "ambiguous", None),
        # Choosing among places, the state decides: the county of the evidence's state is the one.
        ("Monroe County", {"US-NY"}, ["Qcounty_ny", "Qcounty_fl"], "matched", "Qcounty_ny"),
    ],
)
def test_a_place_the_name_belongs_to_competes_for_it(
    name: str, subdivisions: set[str], candidates: list[str], status: str, qid: str | None
) -> None:
    org, business, settlement, county = "Q43229", "Q4830453", "Q486972", oe.COUNTY_CLASS
    client = oe.WikidataClient(oe.RecordedTransport({}))

    def coded(qid: str, prop: str, code: str) -> dict[str, Any]:
        return {**oe.empty_entity(qid), "claims": {prop: [{"rank": "normal", "value": code, "q": {}}]}}

    client.entities = {
        "Qus": coded("Qus", "P297", "US"),
        "Qny": coded("Qny", "P300", "US-NY"),
        "Qid": coded("Qid", "P300", "US-ID"),
        "Qfl": coded("Qfl", "P300", "US-FL"),
        "Qcity_ny": _entity("Qcity_ny", "Syracuse", p31="city", P17="Qus", P131="Qny"),
        "Quniversity": _entity(
            "Quniversity", "Syracuse University", p31="university", aliases=("Syracuse",), P17="Qus"
        ),
        "Qcity_id": _entity("Qcity_id", "Arco", p31="city", P17="Qus", P131="Qid"),
        "Qoilco": _entity("Qoilco", "ARCO", p31="business", P17="Qus"),
        "Qcounty_ny": _entity("Qcounty_ny", "Monroe County", p31="county", P17="Qus", P131="Qny"),
        "Qcounty_fl": _entity("Qcounty_fl", "Monroe County", p31="county", P17="Qus", P131="Qfl"),
    }
    aux = oe._Aux(
        client,
        ancestors={
            "city": {settlement, "Q515", org, "Q783794"},
            "county": {county, "Q15284", "Q56061", org},
            "university": {org, business},
            "business": {org, business},
        },
    )
    target = oe.Target(
        organization_id="o1",
        public_id="org_1",
        name=name,
        keys=(org_key(name),),
        evidence=frozenset({"US"}),
        names=(name,),
        subdivisions=frozenset(subdivisions),
    )
    d = oe.Decision(organization_id="o1", public_id="org_1", name=name, status="no_match")
    got, _entity_found = oe._rule3(target, {"o1": candidates}, aux, oe.MATCH_THRESHOLD, d, required_cik=None)
    assert (d.status if got is None else "matched", got) == (status, qid), d.reasons


@pytest.mark.parametrize(
    ("name", "status", "confidence"),
    [
        ("Prologis Inc", "matched", oe.SEC_CORROBORATED),
        # The operating partnership is not the listed corporation, whatever the CIK's item is called.
        ("Prologis, L.P.", "below_threshold", round(oe.SEC_CORROBORATED * oe.W_LEGAL_FORM_MISMATCH, 4)),
    ],
)
def test_a_corroborated_sec_match_keeps_its_legal_form_markdown(
    name: str, status: str, confidence: float
) -> None:
    cik = "0001045609"
    sec = oe.SecTickers.from_payload(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1045609, "Prologis, Inc.", "PLD", "NYSE"]],
        },
        NOW,
    )
    client = oe.WikidataClient(oe.RecordedTransport({}))
    client.entities = {
        "Qus": {**oe.empty_entity("Qus"), "claims": {"P297": [{"rank": "normal", "value": "US", "q": {}}]}},
        "Qprologis": _entity("Qprologis", "Prologis", p31="business", P17="Qus", P5531=cik),
    }
    aux = oe._Aux(client, ancestors={"business": {"Q43229", "Q4830453"}})
    target = oe.Target(
        organization_id="o1",
        public_id="org_1",
        name=name,
        keys=(org_key(name),),
        evidence=frozenset({"US"}),
        names=(name,),
        subdivisions=frozenset({"US-CA"}),
    )
    score, reasons = oe.score_sec(target, cik, sec)
    lookups = oe._Lookups(
        by_lei={},
        sec_pick={"o1": (cik, score, reasons)},
        sec_rejected={},
        by_cik={cik: ["Qprologis"]},
        candidates={},
        unsearched=set(),
    )
    d = oe._decide(target, lookups, sec, aux, oe.source_licences(), oe.MATCH_THRESHOLD)
    assert (d.status, d.confidence) == (status, confidence), d.reasons
    assert bool(d.fields) is (status == "matched")  # below the threshold nothing is shown


@pytest.mark.parametrize(
    ("name", "subdivisions", "candidates", "status", "qid"),
    [
        # A body Wikidata does not place, and a disambiguation page of its name: the name is shared.
        ("Saint Mary Foundation", {"US-MN"}, ["Qfoundation", "Qdisambig"], "ambiguous", None),
        ("Saint Mary Foundation", {"US-MN"}, ["Qfoundation"], "matched", "Qfoundation"),
        # A business of an ordinary name: a disambiguation page of it says little.
        ("Calpine Corp", {"US-TX"}, ["Qcalpine", "Qdisambig_calpine"], "matched", "Qcalpine"),
        # A local body placed in the evidence's state: the state settles it.
        ("St. Mary Hospital", {"US-MN"}, ["Qhospital_mn", "Qdisambig_hospital"], "matched", "Qhospital_mn"),
        # ...placed in another state: refused, and a local name Wikidata places nowhere too.
        ("St. Mary Hospital", {"US-NY"}, ["Qhospital_mn"], "no_match", None),
        ("St. Mary Hospital", {"US-MN"}, ["Qhospital_nowhere"], "no_match", None),
        # ...unless its description names the state.
        ("St. Mary Hospital", {"US-MN"}, ["Qhospital_named"], "matched", "Qhospital_named"),
    ],
)
def test_a_disambiguation_page_and_a_local_bodys_place(
    name: str, subdivisions: set[str], candidates: list[str], status: str, qid: str | None
) -> None:
    org, business = "Q43229", "Q4830453"
    client = oe.WikidataClient(oe.RecordedTransport({}))

    def coded(qid: str, prop: str, code: str) -> dict[str, Any]:
        return {**oe.empty_entity(qid), "claims": {prop: [{"rank": "normal", "value": code, "q": {}}]}}

    nowhere = _entity("Qhospital_nowhere", "St. Mary Hospital", p31="hospital", P17="Qus")
    nowhere["description"] = "hospital"
    named = _entity("Qhospital_named", "St. Mary Hospital", p31="hospital", P17="Qus")
    named["description"] = "hospital in Rochester, Minnesota"
    client.entities = {
        "Qus": coded("Qus", "P297", "US"),
        "Qmn": coded("Qmn", "P300", "US-MN"),
        "Qfoundation": _entity("Qfoundation", "Saint Mary Foundation", p31="foundation", P17="Qus"),
        "Qdisambig": _entity("Qdisambig", "Saint Mary Foundation", p31="Q4167410"),
        "Qcalpine": _entity("Qcalpine", "Calpine", p31="business", P17="Qus"),
        "Qdisambig_calpine": _entity("Qdisambig_calpine", "Calpine", p31="Q4167410"),
        "Qhospital_mn": _entity("Qhospital_mn", "St. Mary Hospital", p31="hospital", P17="Qus", P131="Qmn"),
        "Qdisambig_hospital": _entity("Qdisambig_hospital", "St. Mary Hospital", p31="Q4167410"),
        "Qhospital_nowhere": nowhere,
        "Qhospital_named": named,
    }
    aux = oe._Aux(
        client,
        ancestors={"foundation": {org}, "hospital": {org}, "business": {org, business}, "Q4167410": set()},
    )
    target = oe.Target(
        organization_id="o1",
        public_id="org_1",
        name=name,
        keys=(org_key(name),),
        evidence=frozenset({"US"}),
        names=(name,),
        subdivisions=frozenset(subdivisions),
    )
    d = oe.Decision(organization_id="o1", public_id="org_1", name=name, status="no_match")
    got, _entity_found = oe._rule3(target, {"o1": candidates}, aux, oe.MATCH_THRESHOLD, d, required_cik=None)
    assert (d.status if got is None else "matched", got) == (status, qid), d.reasons


@pytest.mark.parametrize(
    ("text", "codes", "named"),
    [
        ("public power utility in New York, United States", {"US-NY", "US-TX"}, {"US-NY"}),
        ("Public utility in Texas that manages the lower Colorado River", {"US-TX"}, {"US-TX"}),
        ("independent school in Washington, D.C.", {"US-WA", "US-DC"}, set()),
        ("utility in Seattle, Washington", {"US-WA"}, {"US-WA"}),
        ("electric cooperative in West Virginia", {"US-VA", "US-WV"}, {"US-WV"}),
        ("independent school", {"US-IN"}, set()),
    ],
)
def test_a_description_that_names_the_state(text: str, codes: set[str], named: set[str]) -> None:
    entity = {**oe.empty_entity("Q1"), "labels": {"en": "Some Body"}, "description": text}
    assert oe.named_subdivisions(entity, codes) == named


def test_an_lei_item_with_several_leis_must_carry_the_name() -> None:
    """PSEG's item states PSE&G's LEI beside its own: PSE&G's row, joined by that LEI, would get
    its holding company's facts. An item with several LEIs must also have the row's name key."""
    lei, parent_lei = "549300RUHPVD434XUK18", "PUSS41EMO3E6XXNV3U28"
    client = oe.WikidataClient(oe.RecordedTransport({}))
    pseg = _entity("Qpseg", "Public Service Enterprise Group", p31="business", P17="Qus")
    pseg["claims"]["P1278"] = [{"rank": "normal", "value": v, "q": {}} for v in (lei, parent_lei)]
    client.entities = {
        "Qus": {**oe.empty_entity("Qus"), "claims": {"P297": [{"rank": "normal", "value": "US", "q": {}}]}},
        "Qpseg": pseg,
    }
    aux = oe._Aux(client, ancestors={"business": {"Q43229", "Q4830453"}})
    sec = oe.SecTickers.from_payload({"fields": ["cik", "name", "ticker", "exchange"], "data": []}, NOW)
    lookups = oe._Lookups(
        by_lei={lei: ["Qpseg"]}, sec_pick={}, sec_rejected={}, by_cik={}, candidates={}, unsearched=set()
    )

    def decide(name: str) -> oe.Decision:
        target = oe.Target(
            organization_id="o1",
            public_id="org_1",
            name=name,
            keys=(org_key(name),),
            evidence=frozenset({"US"}),
            names=(name,),
            lei=lei,
        )
        return oe._decide(target, lookups, sec, aux, oe.source_licences(), oe.MATCH_THRESHOLD)

    subsidiary = decide("Public Service Electric and Gas Co")
    assert subsidiary.status == "conflict" and subsidiary.fields == {}
    assert "states 2 LEIs and none of its names" in subsidiary.reasons[0]
    parent = decide("Public Service Enterprise Group Inc")
    assert parent.status == "matched" and _values(parent)["lei"] == lei  # the row's own among the two


@pytest.mark.parametrize(
    ("urls", "website"),
    [
        (["https://www.xcelenergy.com/", "https://my.xcelenergy.com/"], "https://www.xcelenergy.com/"),
        (["https://www.ameren.com/", "https://www.amereninvestors.com/"], "https://www.ameren.com/"),
        (["http://gevo.com", "http://www.gevo.com"], "http://www.gevo.com"),
        (["https://www.kodak.com/", "https://www.kodakalaris.com/"], None),
        (["https://www.duke-energy.com/"], "https://www.duke-energy.com/"),
    ],
)
def test_one_official_website_or_none(urls: list[str], website: str | None) -> None:
    keys = [org_key(n) for n in ("Xcel Energy", "Ameren", "Gevo", "Eastman Kodak", "Duke Energy")]
    reasons: list[str] = []
    assert oe.official_website(urls, keys, reasons) == website
    assert bool(reasons) is (website is None)


def test_the_searches_reach_every_organisation_in_turn() -> None:
    """Half the budget on the largest every month, the other half moving on through the rest, so
    the smallest are not left unsearched month after month."""
    queue = [
        oe.Target(
            organization_id=str(n),
            public_id=f"org_{n}",
            name=f"Org {n}",
            keys=(f"org {n}",),
            evidence=frozenset(),
        )
        for n in range(10)
    ]
    ids = lambda ts: [t.organization_id for t in ts]  # noqa: E731
    assert ids(oe.search_order(queue, budget=4, rotation=0)) == [str(n) for n in range(10)]
    month1 = ids(oe.search_order(queue, budget=4, rotation=1))
    assert month1[:2] == ["0", "1"] and month1[2:4] == ["4", "5"]
    reached = {i for r in range(4) for i in ids(oe.search_order(queue, budget=4, rotation=r))[:4]}
    assert reached == {str(n) for n in range(10)}


# --------------------------------------------------------------------------- re-recording
class _FixtureRecorder:
    """Live answers, except the SEC file (cut to `keys`) and the searches already recorded."""

    def __init__(self, inner: oe.Transport, keys: set[str], searches: Mapping[str, Any]) -> None:
        self.inner, self.keys, self.searches = inner, keys, searches
        self.exchanges: list[dict[str, Any]] = []

    def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        if url == oe.WIKIDATA_API_URL and params.get("search") in self.searches:
            payload = self.searches[params["search"]]
        else:
            payload = self.inner.get_json(url, params)
        if url == oe.SEC_TICKERS_URL:
            name_at, cik_at = payload["fields"].index("name"), payload["fields"].index("cik")
            payload = {
                "fields": payload["fields"],
                "data": [
                    r
                    for r in payload["data"]
                    if org_key(oe.sec_core_name(str(r[name_at]))) in self.keys
                    or int(r[cik_at]) in EXTRA_SEC_CIKS
                ],
            }
        self.exchanges.append({"url": url, "params": dict(params), "response": payload})
        return payload


def record_fixture(searches_path: pathlib.Path | None = None) -> None:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    searches: dict[str, Any] = {}
    if searches_path is not None:
        searches = {
            k: v["response"] for k, v in json.loads(searches_path.read_text(encoding="utf-8")).items()
        }
    with get_sessionmaker(engine)() as s:
        build_store(s)
        targets = oe.collect_targets(s)
        keys = {k for t in targets for k in t.keys}
        recorder = _FixtureRecorder(oe.LiveTransport(), keys, searches)
        result = oe.fetch_profiles(None, recorder, targets=targets, clock=lambda: NOW)
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(
        json.dumps(
            {
                "note": __doc__.split("\n\n")[1] if __doc__ else "",
                "recorded_at": oe.iso(oe.utcnow()),
                "exchanges": recorder.exchanges,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(json.dumps(result.report["by_status"]) + f" {len(recorder.exchanges)} exchanges\n")


if __name__ == "__main__":
    if "--record" in sys.argv:
        extra = [a for a in sys.argv[1:] if a != "--record"]
        record_fixture(pathlib.Path(extra[0]) if extra else None)
