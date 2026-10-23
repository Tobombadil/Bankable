"""The loader's value fields (`services/ingest/opportunity_value.py`, lane rfp-value): every load
completes `identifiers["value"]` with the USD equivalent at the rate of the notice's publication
date, and `restate_values` brings rows loaded before the lane to the same shape without moving
`last_changed`. Recorded fixtures only: the TED search response and the ECB history slice."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import connector_for, fixture_path, snapshot
from pipeline import fx
from pipeline.connectors.registry import Registry
from services.db.models import Opportunity, OpportunitySource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader
from services.ingest import opportunity_value as ov

TS = "20260912T180000Z"
TED_URL = "https://api.ted.europa.eu/v3/notices/search"


@pytest.fixture(scope="module")
def table() -> fx.RateTable:
    text = fixture_path("ecb_eurofxref_hist.csv").read_text(encoding="utf-8")
    return fx.RateTable(fx.parse_hist_csv(text), retrieved_at="2026-10-22T01:01:38Z", origin="fetched")


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


@pytest.fixture()
def rates(monkeypatch: pytest.MonkeyPatch, table: fx.RateTable) -> fx.RateTable:
    """The loader reads the fixture table, whatever data root the machine has."""
    monkeypatch.setattr(ov.fx, "default_table", lambda: table)
    return table


def test_a_euro_amount_gains_its_equivalent_at_the_publication_date(table: fx.RateTable) -> None:
    fields = {
        "budget_amount": 1_000_000.0,
        "budget_currency": "EUR",
        "open_at": dt.date(2026, 10, 6),
        "identifiers": {
            "ted_notice_id": "x",
            "value": {"kind": "estimated_value", "source_field": "total-value"},
        },
    }
    out = ov.with_value(fields, table)
    assert out["identifiers"]["ted_notice_id"] == "x"
    assert out["identifiers"]["value"] == {
        "kind": "estimated_value",
        "source_field": "total-value",
        "amount": 1_000_000.0,
        "currency": "EUR",
        "usd_equivalent": 1_126_900.0,
        "fx_rate": 1.1269,
        "fx_date": "2026-10-06",
        "fx_source": "eu.ecb.eurofxref",
        "fx_retrieved_at": "2026-10-22T01:01:38Z",
    }
    assert fields["identifiers"]["value"] == {
        "kind": "estimated_value",
        "source_field": "total-value",
    }  # not mutated


def test_usd_unquoted_and_absent_amounts(table: fx.RateTable) -> None:
    usd = ov.with_value(
        {"budget_amount": 5e6, "budget_currency": "USD", "open_at": None, "identifiers": {}}, table
    )
    assert usd["identifiers"]["value"]["usd_equivalent"] == 5e6
    assert usd["identifiers"]["value"]["fx_source"] is None and usd["identifiers"]["value"]["fx_date"] is None
    mkd = ov.with_value({"budget_amount": 10_000_000.0, "budget_currency": "MKD", "identifiers": {}}, table)
    assert mkd["identifiers"]["value"]["usd_equivalent"] is None
    assert mkd["identifiers"]["value"]["fx_note"] == "no ECB reference rate for MKD"
    none = ov.with_value(
        {"budget_amount": None, "identifiers": {"value": {"kind": "estimated_value"}}}, table
    )
    assert "value" not in none["identifiers"]  # no amount, no value: nothing is inferred


@pytest.mark.parametrize(
    ("amount", "currency", "reason"),
    [
        (2_147_483_647, "USD", "largest number"),  # Grants.gov 363912's estimatedFunding
        (999_999_999, "USD", "run of nines"),  # Grants.gov 361080's award ceiling
        (0.26, "EUR", "unit price"),  # a TED electricity concession's "awarded value"
        (1, "EUR", "unit price"),
    ],
)
def test_a_placeholder_amount_is_kept_as_stated_but_not_ranked(
    table: fx.RateTable, amount: float, currency: str, reason: str
) -> None:
    out = ov.with_value(
        {
            "budget_amount": amount,
            "budget_currency": currency,
            "open_at": dt.date(2026, 10, 6),
            "identifiers": {},
        },
        table,
    )
    block = out["identifiers"]["value"]
    assert block["amount"] == float(amount) and out["budget_amount"] == amount  # never altered
    assert block["usd_equivalent"] is None and block["fx_source"] is None
    assert reason in block["amount_note"]
    # Ordinary figures, round or not, are ranked as stated; a ceiling set just under a threshold
    # ($999,999, $9,999,999) is a figure, not a run-of-nines placeholder (review 2026-10-22).
    for ordinary in (100, 1_000_000, 999_999.5, 99_999, 999_999, 9_999_999, 99_999_999):
        normal = ov.with_value(
            {"budget_amount": ordinary, "budget_currency": "USD", "identifiers": {}}, table
        )
        assert normal["identifiers"]["value"]["usd_equivalent"] == float(ordinary)
        assert "amount_note" not in normal["identifiers"]["value"]


def test_a_reload_replaces_the_previous_conversion(table: fx.RateTable) -> None:
    first = ov.with_value(
        {
            "budget_amount": 10_000.0,
            "budget_currency": "RON",
            "open_at": dt.date(2026, 9, 6),
            "identifiers": {},
        },
        table,
    )
    assert first["identifiers"]["value"]["fx_date"] == "2026-09-04"  # Sunday -> Friday's fixing
    second = ov.with_value({**first, "budget_amount": 20_000.0, "open_at": dt.date(2026, 10, 21)}, table)
    block = second["identifiers"]["value"]
    assert (block["amount"], block["fx_date"]) == (20_000.0, "2026-10-21")
    assert block["usd_equivalent"] == round(20_000 * 1.1234 / 5.3365, 2)


def _ted_frame() -> pd.DataFrame:
    connector = connector_for("eu.ted.api")
    raw = snapshot("ted_search.json", TED_URL, "application/json")
    return connector.normalize(connector.parse(raw), raw)


def _load_ted(session: Session, root: pathlib.Path) -> None:
    out = root / "normalized" / "eu.ted.api"
    out.mkdir(parents=True)
    _ted_frame().to_parquet(out / f"{TS}.parquet", index=False)
    loader.load_from_files(session, "eu.ted.api", TS, data_root=root, registry=Registry())
    session.flush()


def _by_notice(session: Session) -> dict[str, Opportunity]:
    out: dict[str, Opportunity] = {}
    for opp in session.scalars(select(Opportunity)):
        out[str(opp.identifiers["ted_notice_id"])] = opp
    return out


def test_a_ted_load_stores_kind_amount_and_usd_equivalent(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable
) -> None:
    _load_ted(session, tmp_path)
    rows = _by_notice(session)
    award = rows["619292-2026"]  # a result notice, PLN 4,673,237.67, published 2026-09-09
    block = award.identifiers["value"]
    assert (float(award.budget_amount), award.budget_currency) == (4_673_237.67, "PLN")
    assert (block["kind"], block["source_field"]) == ("awarded_value", "total-value")
    # 2026-09-09 is not in the fixture slice: the fixing on or before it is 2026-09-07.
    expected = rates.convert(4_673_237.67, "PLN", dt.date(2026, 9, 9))
    assert expected is not None and expected.fx_date == dt.date(2026, 9, 7)
    assert block["usd_equivalent"] == expected.usd_equivalent and block["fx_date"] == "2026-09-07"
    # The link's own row says the same, so a gated re-derivation serves the same value.
    link = session.scalars(
        select(OpportunitySource).where(OpportunitySource.opportunity_id == award.id)
    ).one()
    assert link.normalised["identifiers"]["value"]["usd_equivalent"] == expected.usd_equivalent
    # Provenance for the amount is stamped like every other field.
    assert award.field_provenance["budget_amount"]["source_id"] == "eu.ted.api"
    # A notice stating no value has none.
    assert "value" not in rows["619389-2026"].identifiers


def test_restating_rows_loaded_before_the_lane_fills_the_value_without_news(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable
) -> None:
    _load_ted(session, tmp_path)
    rows = _by_notice(session)
    stamps = {}
    for key, opp in rows.items():  # the shape a pre-lane load left
        ids = dict(opp.identifiers)
        ids.pop("value", None)
        opp.identifiers = ids
        stamps[key] = opp.last_changed
    # The 2026-10-20 store's estimate-list notice, before the parser read the list.
    award = rows["619292-2026"]
    link = session.scalars(
        select(OpportunitySource).where(OpportunitySource.opportunity_id == award.id)
    ).one()
    link.raw = {**link.raw, "estimated-value-glo": ["3290325.87"], "estimated-value-cur-glo": ["PLN"]}
    # An operator's pinned amount is left as pinned and converted as pinned.
    pinned = rows["619362-2026"]
    pinned.budget_amount, pinned.overrides = 1_000_000, {"budget_amount": {"by": "operator"}}
    session.flush()

    result = ov.restate_values(session, rates)

    assert result.examined == len(rows) and result.changed == result.with_value == 5
    assert result.by_kind["awarded_value"] >= 1 and result.no_rate == {}
    restated = award.identifiers["value"]
    assert restated["kind"] == "awarded_value"
    assert restated["also_stated"] == [
        {
            "kind": "estimated_value",
            "amount": 3_290_325.87,
            "currency": "PLN",
            "source_field": "estimated-value-glo",
        }
    ]
    assert restated["usd_equivalent"] is not None
    kept = pinned.identifiers["value"]
    assert (float(pinned.budget_amount), kept["amount"]) == (1_000_000.0, 1_000_000.0)
    assert kept["usd_equivalent"] == rates.convert(1_000_000, "DKK", dt.date(2026, 9, 9)).usd_equivalent  # type: ignore[union-attr]
    assert {k: o.last_changed for k, o in rows.items()} == stamps  # a restatement is not news
    # The link is the source's own view: the pinned amount is the record's, never the link's.
    source_amount = float(_ted_frame().set_index("source_record_id").loc["619362-2026", "budget_amount"])
    pinned_link = session.scalars(
        select(OpportunitySource).where(OpportunitySource.opportunity_id == pinned.id)
    ).one()
    assert pinned_link.normalised["budget_amount"] == source_amount != 1_000_000
    assert pinned_link.normalised["identifiers"]["value"]["amount"] == source_amount
    # Idempotent: a second pass changes nothing.
    again = ov.restate_values(session, rates)
    assert (again.changed, again.links_restated) == (0, 0)


def test_restating_corrects_an_amount_the_old_parser_missed(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable
) -> None:
    """A competition notice whose only figure was the estimate list had no budget before the lane;
    the restatement reads it from the stored raw row and credits that row's source."""
    _load_ted(session, tmp_path)
    opp = _by_notice(session)["619389-2026"]
    assert opp.budget_amount is None
    link = session.scalars(select(OpportunitySource).where(OpportunitySource.opportunity_id == opp.id)).one()
    link.raw = {**link.raw, "estimated-value-glo": ["882600"], "estimated-value-cur-glo": ["EUR"]}
    session.flush()
    result = ov.restate_values(session, rates)
    assert result.amounts_restated == 1
    assert (float(opp.budget_amount), opp.budget_currency) == (882_600.0, "EUR")
    assert opp.identifiers["value"]["kind"] == "estimated_value"
    assert opp.field_provenance["budget_amount"]["source_id"] == "eu.ted.api"
    assert link.normalised["budget_amount"] == 882_600.0


def test_what_is_news_leaves_out_read_times_and_the_conversion() -> None:
    identifiers = {
        "grants_gov_id": 363396,
        "value": {
            "kind": "total_programme_funding",
            "amount": 410_000_000.0,
            "currency": "USD",
            "retrieved_at": "2026-10-22T01:00:00Z",
            "usd_equivalent": 410_000_000.0,
            "fx_rate": 1.0,
            "fx_date": None,
            "fx_source": None,
            "fx_retrieved_at": "2026-10-22T01:01:38Z",
        },
    }
    assert ov.served_identifiers(identifiers) == {
        "grants_gov_id": 363396,
        "value": {"kind": "total_programme_funding", "amount": 410_000_000.0, "currency": "USD"},
    }
    assert ov.served_identifiers({"cpv": ["09310000"]}) == {"cpv": ["09310000"]}
    assert ov.served_identifiers(None) is None


def _load_frame(
    session: Session, root: pathlib.Path, frame: pd.DataFrame, ts: str, source_id: str = "eu.ted.api"
) -> loader.LoadResult:
    out = root / "normalized" / source_id
    out.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out / f"{ts}.parquet", index=False)
    result = loader.load_from_files(session, source_id, ts, data_root=root, registry=Registry())
    session.flush()
    assert isinstance(result, loader.LoadResult)
    return result


def _restamped(identifiers: str, at: str) -> str:
    doc = json.loads(identifiers)
    if "value" in doc:
        doc["value"]["retrieved_at"] = at
    return json.dumps(doc)


def test_a_reload_that_moves_only_read_times_and_rates_is_not_news(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of lane rfp-value (2026-10-22): `identifiers` is a served field, so a reload
    whose figures had not changed but whose read time had (a Grants.gov detail re-read a week
    later) or whose rate table had been refreshed moved `last_changed` on every notice with a value:
    105 of 105 on the study store. Measured here: neither moves it; a changed amount still does."""
    frame = _ted_frame()
    _load_frame(session, tmp_path, frame, TS)
    rows = _by_notice(session)
    stamps = {k: o.last_changed for k, o in rows.items()}
    assert sum("value" in o.identifiers for o in rows.values()) == 5

    # A week on: the figures read again at a new time, and a refreshed table that now holds the
    # 2026-09-09 fixing (the award converted at 2026-09-07's) at a different rate.
    later = frame.copy()
    later["identifiers"] = later["identifiers"].map(lambda ids: _restamped(ids, "2026-10-29T01:00:00Z"))
    fixings = dict(rates.fixings)
    fixings[dt.date(2026, 9, 9)] = {**rates.fixings[dt.date(2026, 9, 7)], "USD": 1.2}
    refreshed = fx.RateTable(fixings, retrieved_at="2026-10-29T16:19:00Z", origin="fetched")
    monkeypatch.setattr(ov.fx, "default_table", lambda: refreshed)
    result = _load_frame(session, tmp_path, later, "20260919T180000Z")
    award = rows["619292-2026"].identifiers["value"]
    assert (award["fx_date"], award["fx_retrieved_at"], award["retrieved_at"]) == (
        "2026-09-09",
        "2026-10-29T16:19:00Z",
        "2026-10-29T01:00:00Z",
    )  # the stored block is current ...
    assert result.records_changed == 0  # ... and none of it is news
    assert {k: o.last_changed for k, o in rows.items()} == stamps

    # A changed figure is news.
    changed = later.copy()
    changed.loc[changed["source_record_id"] == "619292-2026", "budget_amount"] = 5_000_000.0
    result = _load_frame(session, tmp_path, changed, "20260926T180000Z")
    assert result.records_changed == 1
    assert rows["619292-2026"].last_changed != stamps["619292-2026"]


def _strip_values(rows: dict[str, Opportunity]) -> None:
    """The shape a load before the lane left: no value block on the record or its link."""
    for opp in rows.values():
        ids = dict(opp.identifiers)
        ids.pop("value", None)
        opp.identifiers = ids
        for link in opp.sources:
            normalised = dict(link.normalised or {})
            own = dict(normalised.get("identifiers") or {})
            own.pop("value", None)
            link.normalised = {**normalised, "identifiers": own}


def test_the_reload_after_the_restatement_is_not_news_and_without_it_is_once(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable
) -> None:
    """Review of lane rfp-value (2026-10-22), backfill ordering: the lane changes every opportunity
    connector's parser digest, so each source's next run re-derives its stored rows and reloads them
    with their value blocks. A row the restatement reached first reloads unchanged; a row it did not
    reach moves `last_changed` once, which is why the deploy runbook (docs/63 §4.1) runs it before
    the sources' next runs."""
    frame = _ted_frame()
    _load_frame(session, tmp_path, frame, TS)
    rows = _by_notice(session)
    _strip_values(rows)
    session.flush()
    stamps = {k: o.last_changed for k, o in rows.items()}

    restated = ov.restate_values(session, rates)
    assert restated.changed == 5
    result = _load_frame(session, tmp_path, frame, "20261023T010000Z")
    assert result.records_changed == 0
    assert {k: o.last_changed for k, o in rows.items()} == stamps

    # The other order: the reload reaches the rows first, and each row with a value moves once.
    _strip_values(rows)
    session.flush()
    result = _load_frame(session, tmp_path, frame, "20261024T010000Z")
    assert result.records_changed == 5
    assert sum(o.last_changed != stamps[k] for k, o in rows.items()) == 5


GG = "us.grants_gov.search2"
GG_URL = "https://api.grants.gov/v1/api/search2"


def _gg_frame(read_at: str, funding: str | None = None) -> pd.DataFrame:
    """The Grants.gov search fixture normalised with the recorded details read at `read_at`, as a
    run before the confirmation rule stored them: every re-read restamped (lane rfp-value v1)."""
    from pipeline.connectors.us_grants_gov_search2.connector import detail_subset

    connector = connector_for(GG)
    raw = snapshot(
        "grants_gov_search2.json", GG_URL, "application/json", dt.datetime(2026, 10, 22, 1, tzinfo=dt.UTC)
    )
    doc = json.loads(raw.content)
    hits = {str(h["id"]): h for page in doc["pages"] for h in page["data"]["oppHits"]}
    responses = json.loads(fixture_path("grants_gov_fetch_opportunity.json").read_text(encoding="utf-8"))
    if funding is not None:
        responses["363396"]["data"]["synopsis"]["estimatedFunding"] = funding
    doc["details"] = {i: detail_subset(r, read_at, hits[i]) for i, r in responses.items() if i in hits}
    raw.content = json.dumps(doc).encode()
    return connector.normalize(connector.parse(raw), raw)


def test_a_grants_gov_detail_read_again_a_week_later_is_not_news(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable
) -> None:
    """Review of lane rfp-value (2026-10-22), as reproduced there: two loads of the same
    Grants.gov figures 8 days apart, the second with each detail's new `retrieved_at`, moved
    `last_changed` on 4 of the 4 notices with figures (`records_changed` 4), so about 136 false
    updates a week would reach `updated_since`, alert windows, sort=-last_changed and the feed. The
    read time is not news (`served_identifiers`); a changed figure is."""
    _load_frame(session, tmp_path, _gg_frame("2026-10-22T01:00:00Z"), "20261022T010000Z", GG)
    rows = {str(o.identifiers["grants_gov_id"]): o for o in session.scalars(select(Opportunity))}
    valued = {k for k, o in rows.items() if "value" in o.identifiers}
    assert valued == {"279638", "356623", "361526", "363396"}
    stamps = {k: o.last_changed for k, o in rows.items()}

    week = _load_frame(session, tmp_path, _gg_frame("2026-10-30T01:00:00Z"), "20261030T010000Z", GG)
    assert week.records_changed == 0
    assert {k: o.last_changed for k, o in rows.items()} == stamps
    assert rows["363396"].identifiers["value"]["retrieved_at"] == "2026-10-30T01:00:00Z"  # stored, not news

    revised = _gg_frame("2026-11-07T01:00:00Z", funding="420000000")
    result = _load_frame(session, tmp_path, revised, "20261107T010000Z", GG)
    assert result.records_changed == 1 and float(rows["363396"].budget_amount) == 420_000_000.0
    assert rows["363396"].last_changed != stamps["363396"]
    assert all(rows[k].last_changed == stamps[k] for k in rows if k != "363396")


def test_a_pinned_amount_is_converted_as_pinned_and_the_link_keeps_the_source_amount(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable
) -> None:
    """Review of lane rfp-value: a load built the stored block from the source row even when an
    operator had pinned `budget_amount`, so `sort=value` ranked a figure the record did not show."""
    frame = _ted_frame()
    _load_frame(session, tmp_path, frame, TS)
    award = _by_notice(session)["619292-2026"]
    award.budget_amount, award.overrides = 1_000_000, {"budget_amount": {"by": "operator"}}
    session.flush()
    _load_frame(session, tmp_path, frame, "20260919T180000Z")
    block = award.identifiers["value"]
    expected = rates.convert(1_000_000, "PLN", dt.date(2026, 9, 9))
    assert expected is not None
    assert (float(award.budget_amount), block["amount"], block["usd_equivalent"]) == (
        1_000_000.0,
        1_000_000.0,
        expected.usd_equivalent,
    )
    assert block["kind"] == "awarded_value"  # the connector's account of the figure stays
    link = session.scalars(
        select(OpportunitySource).where(OpportunitySource.opportunity_id == award.id)
    ).one()
    assert link.normalised["budget_amount"] == 4_673_237.67
    assert link.normalised["identifiers"]["value"]["amount"] == 4_673_237.67


def test_an_amount_is_kept_and_converted_to_the_cent_as_the_column_stores_it(table: fx.RateTable) -> None:
    """`budget_amount` is Numeric(18, 2); the block rounds the same way, so the served amount and the
    one the list sorts on are one figure. 0 and less are a source's "not stated"."""
    fields = {"budget_amount": 1_234.567, "budget_currency": "EUR", "open_at": dt.date(2026, 10, 6)}
    block = ov.with_value({**fields, "identifiers": {}}, table)["identifiers"]["value"]
    assert block["amount"] == 1_234.57 and block["usd_equivalent"] == round(1_234.57 * 1.1269, 2)
    for nothing in (0, -1, float("nan"), "n/a"):
        assert (
            "value" not in ov.with_value({"budget_amount": nothing, "identifiers": {}}, table)["identifiers"]
        )


def test_the_restatement_locks_rows_on_postgres_and_skips_those_a_load_holds() -> None:
    from sqlalchemy.dialects import postgresql, sqlite

    pg = str(ov.restate_statement().compile(dialect=postgresql.dialect()))
    assert pg.rstrip().endswith("FOR UPDATE OF opportunity SKIP LOCKED")
    assert "FOR UPDATE" not in str(ov.restate_statement().compile(dialect=sqlite.dialect()))


def test_the_daily_pass_reports_a_failed_rate_fetch_and_restates_anyway(
    session: Session, tmp_path: pathlib.Path, rates: fx.RateTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    _load_ted(session, tmp_path)
    for opp in _by_notice(session).values():  # the shape a pre-lane load left
        opp.identifiers = {k: v for k, v in opp.identifiers.items() if k != "value"}
    session.flush()
    monkeypatch.setattr(ov.fx, "load_table", lambda *args, **kwargs: rates)

    def offline() -> fx.RefreshResult:
        raise fx.FxError("not the ECB history archive")

    report = ov.value_pass(session, _refresh=offline)
    assert report["fx_refresh"]["status"] == "failed" and "FxError" in report["fx_refresh"]["error"]
    assert (report["changed"], report["with_value"]) == (5, 5)
    assert report["fx_table"] == {
        "origin": "fetched",
        "retrieved_at": "2026-10-22T01:01:38Z",
        "last_fixing": "2026-10-21",
    }
