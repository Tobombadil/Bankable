"""The loader and statistics job over a store the real runner built from the fixtures
(`conftest.py`): catalogue rows, daily summaries, latest only where raw is allowed, retail."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
import sqlalchemy as sa

from pipeline.connectors.prices import store as pstore
from pipeline.connectors.prices.locations import LOCATIONS
from pipeline.connectors.store import Store
from services.db.models import Source
from services.prices.daily import summarise
from services.prices.load import load_intervals, load_run, load_source
from services.prices.models import PriceDaily, PriceLatest, PriceLocation, PriceStat, RetailPrice


def test_every_catalogue_location_is_a_row_with_its_placement(price_session) -> None:
    session, _ = price_session
    rows = {r.id: r for r in session.scalars(sa.select(PriceLocation))}
    assert set(rows) == {loc.id for loc in LOCATIONS}
    for loc in LOCATIONS:
        row = rows[loc.id]
        assert row.placement_grade == loc.placement.grade and row.region_id == loc.placement.region_id
        assert (row.lat is None) == (loc.placement.grade == "region")
        assert row.source_id == loc.source_id and row.licence_id and row.source_url and row.retrieved_at
    assert rows["ercot.lz_west"].reference_id == "ercot.hb_north"
    assert rows["ercot.hb_north"].reference_id == "ercot.hb_north"
    assert rows["nyiso.capitl"].reference_id is None


def test_latest_rows_exist_only_where_raw_publication_is_allowed(price_session) -> None:
    session, reports = price_session
    sources = {r.location_id.split(".")[0] for r in session.scalars(sa.select(PriceLatest))}
    assert sources == {"ercot", "spp", "gb"}
    assert reports["us.iso.caiso.oasis_prices"]["latest_rows"] == 0
    assert reports["us.iso.nyiso.prices"]["latest_rows"] == 0
    north = session.get(PriceLatest, ("ercot.hb_north", "rt"))
    assert north is not None and north.interval_start.date() == dt.date(2026, 10, 22)
    assert session.get(PriceLatest, ("spp.sppnorth_hub", "rt")) is not None


def test_daily_rows_carry_provenance_and_market_local_days(price_session) -> None:
    session, _ = price_session
    row = session.get(PriceDaily, ("ercot.hb_north", "rt", dt.date(2026, 10, 21)))
    assert row is not None and row.intervals == row.expected_intervals == 96
    assert row.source_id == "us.iso.ercot.prices" and row.source_url.endswith("20261021_real_time_spp.html")
    assert row.top4_mean is not None and row.top4_mean >= row.bottom4_mean
    zone = session.get(PriceDaily, ("ercot.lz_west", "rt", dt.date(2026, 10, 21)))
    assert zone is not None and zone.basis_intervals == 96 and zone.basis_mean is not None
    gb = session.get(PriceDaily, ("gb.system", "rt", dt.date(2026, 10, 21)))
    assert gb is not None and gb.intervals == 48


def test_statistics_withhold_what_two_days_of_history_cannot_support(price_session) -> None:
    session, reports = price_session
    assert reports["stats"]["stats_written"] > 0
    stat = session.get(PriceStat, ("ercot.lz_west", "rt", "trailing_12m"))
    assert stat is not None
    assert stat.window_end == dt.date(2026, 10, 21) and stat.days >= 1
    # One complete day held: the window is that day, so coverage is complete and the mean stands.
    assert stat.mean is not None and stat.sample_count == stat.expected_count
    assert stat.basis_reference_id == "ercot.hb_north" and stat.basis_mean is not None
    ref = session.get(PriceStat, ("ercot.hb_north", "rt", "trailing_12m"))
    assert ref is not None and ref.basis_mean is None and "reference hub" in ref.withheld["basis_mean"]
    nyiso = session.get(PriceStat, ("nyiso.capitl", "da", "trailing_12m"))
    assert (
        nyiso is not None and nyiso.basis_mean is None and "no reference hub" in nyiso.withheld["basis_mean"]
    )


def test_retail_rows_and_release_date(price_session) -> None:
    session, reports = price_session
    assert reports["us.eia.retail_sales"]["retail_rows"] == 6 * 13 * 5
    tx = session.scalars(
        sa.select(RetailPrice).where(RetailPrice.state == "TX", RetailPrice.sector == "industrial")
    ).all()
    assert len(tx) == 13 and {r.released for r in tx} == {"2026-09-24"}
    assert all(r.source_id == "us.eia.retail_sales" and r.retrieved_at for r in tx)


def test_price_sources_are_marked_implemented(price_session) -> None:
    session, _ = price_session
    for source_id in ("us.iso.ercot.prices", "us.eia.retail_sales", "us.iso.spp.prices"):
        source = session.get(Source, source_id)
        assert source is not None and source.implemented is True


def test_loading_twice_changes_nothing(price_session, price_store) -> None:
    session, _ = price_session
    before = session.scalar(sa.select(sa.func.count()).select_from(PriceDaily))
    load_source(session, "us.iso.ercot.prices", store=price_store)
    session.flush()
    assert session.scalar(sa.select(sa.func.count()).select_from(PriceDaily)) == before


def test_load_run_reads_the_months_the_run_wrote(price_session, price_store) -> None:
    session, _ = price_session
    runs = sorted(p.stem for p in (price_store.root / "runs" / "gb.elexon.bmrs_prices").glob("*.json"))
    report = load_run(session, "gb.elexon.bmrs_prices", runs[-1], store=price_store)
    assert report["daily_rows"] >= 1


def test_a_record_source_is_refused() -> None:
    with pytest.raises(KeyError):
        load_source(None, "us.iso.ercot.gen_queue")  # type: ignore[arg-type]


def test_the_load_command_loads_one_run_and_says_so(tmp_path, monkeypatch, price_store, capsys) -> None:
    """`python -m services.prices.load <source> --run <ts>`, the scheduler's load process
    (`infra/scheduler/prices.py::price_load_job`): exit 0 with the report as a JSON line, exit 1
    with the error for a run that is not there."""
    import json

    from services.db.session import get_engine, init_db
    from services.prices import load as load_module

    url = f"sqlite+pysqlite:///{tmp_path / 'load.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = get_engine(url)
    init_db(engine)
    engine.dispose()
    monkeypatch.setattr(load_module, "open_store", lambda *_a: price_store)
    ts = sorted(p.stem for p in (price_store.root / "runs" / "gb.elexon.bmrs_prices").glob("*.json"))[-1]
    assert load_module.main(["gb.elexon.bmrs_prices", "--run", ts]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["source_id"] == "gb.elexon.bmrs_prices" and report["daily_rows"] >= 1
    assert load_module.main(["gb.elexon.bmrs_prices", "--run", "19990101T000000Z"]) == 1
    assert "error" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --------------------------------------------------------------- memory: chunks, columns, latest
def _gb_store(root) -> tuple[Store, pd.DataFrame]:
    """Half-hourly GB prices 2026-09-28 .. 2026-10-03 (British Summer Time: local 1 October starts
    at 23:00 UTC on 30 September), written as the runner writes them, one file per UTC month."""
    store = Store(root)
    starts = pd.date_range("2026-09-27T23:00Z", "2026-10-03T22:30Z", freq="30min")
    frame = pd.DataFrame(
        {
            "record_id": [f"gb.elexon.bmrs_prices:SYSTEM|rt|{s:%Y-%m-%dT%H:%M:%SZ}" for s in starts],
            "source_id": "gb.elexon.bmrs_prices",
            "source_record_id": [f"SYSTEM|rt|{s:%Y-%m-%dT%H:%M:%SZ}" for s in starts],
            "source_url": "https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/system-prices/x",
            "retrieved_at": "2026-10-04T00:00:00Z",
            "licence_id": "gb-bmrs-open",
            "location_id": "gb.system",
            "location": "SYSTEM",
            "market": "rt",
            "interval_start": starts,
            "interval_end": starts + pd.Timedelta(minutes=30),
            "interval_minutes": 30,
            "price": [float(i % 97) - 10.0 for i in range(len(starts))],
            "currency": "GBP",
            "raw": "{}",
        }
    )
    for month, part in frame.groupby(frame["interval_start"].dt.strftime("%Y-%m")):
        pstore.write_month(store, "gb.elexon.bmrs_prices", str(month), part)
    return store, frame


def test_month_chunks_give_the_whole_frame_s_summaries(price_session, tmp_path) -> None:
    """One local month at a time over three UTC months equals one pass over every row, including
    the local day that begins in the UTC month before."""
    session, _ = price_session
    store, frame = _gb_store(tmp_path)
    licence = session.get(PriceLocation, "gb.system").licence_id
    report = load_intervals(session, store, "gb.elexon.bmrs_prices", licence, None)
    assert report["daily_rows"] == 6  # local 28 Sep .. 3 Oct
    whole = summarise(frame, {"gb.system": "Europe/London"}, {"gb.system": None})
    for rec in whole.to_dict("records"):
        row = session.get(PriceDaily, ("gb.system", "rt", rec["day"]))
        assert row is not None
        assert (row.intervals, row.mean, row.min, row.max) == (
            rec["intervals"],
            pytest.approx(rec["mean"]),
            rec["min"],
            rec["max"],
        )
    first_october = session.get(PriceDaily, ("gb.system", "rt", dt.date(2026, 10, 1)))
    assert first_october is not None and first_october.intervals == first_october.expected_intervals == 48


def test_the_loader_never_reads_raw_rows(price_session, price_store, monkeypatch) -> None:
    session, _ = price_session
    asked: list[list[str] | None] = []
    real = pstore.read_month

    def spy(store, source_id, month, columns=None):
        asked.append(columns)
        return real(store, source_id, month, columns)

    monkeypatch.setattr(pstore, "read_month", spy)
    load_source(session, "us.iso.ercot.prices", store=price_store)
    assert asked and all(cols is not None and "raw" not in cols and "record_id" not in cols for cols in asked)


def test_a_day_ahead_hour_that_has_not_begun_is_not_the_latest(price_session, price_store) -> None:
    """ERCOT's day-ahead page for 22 October is stored by midday on the 21st's operating day; at
    10:00 UTC on the 22nd the latest day-ahead price is that hour's, not hour ending 24's."""
    session, _ = price_session
    at = dt.datetime(2026, 10, 22, 10, 0, tzinfo=dt.UTC)
    load_source(session, "us.iso.ercot.prices", store=price_store, now=at)
    row = session.get(PriceLatest, ("ercot.hb_north", "da"))
    assert row is not None
    assert row.interval_start.replace(tzinfo=dt.UTC) == at
    later = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)
    load_source(session, "us.iso.ercot.prices", store=price_store, now=later)
    row = session.get(PriceLatest, ("ercot.hb_north", "da"))
    assert row is not None and row.interval_start.replace(tzinfo=dt.UTC) == dt.datetime(
        2026, 10, 23, 4, 0, tzinfo=dt.UTC
    )  # hour ending 24 Central of 22 October, once it has begun
