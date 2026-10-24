"""Loading Ember runs into the global electricity tables, and reading the figures back (offline fixtures).

Held in place: every value lands with its provenance quartet; loading twice changes nothing; a new run of a
dataset replaces its previous load whole, and a failed load or a failed download leaves the previous figures
served; a run fetched under other terms is refused; the source is marked implemented; the reader's latest
year, year-on-year change, clean share, coverage and wind and solar ranking follow their definitions; a
figure comes from the load with the newest period, the API's on a tie, never from an older download copied in
later; a total's or a share's change compares only years carrying the same generation types.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import services.global_figures.models  # noqa: F401 - registers the tables before the template is built
from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.global_ember_electricity.runner import ENV_KEY, EmberRun
from pipeline.connectors.global_ember_electricity.testing import (
    PATH_FIXTURES,
    TEST_KEY,
    YEARLY_JSON,
    FakeSession,
    fixed_clock,
    fixture_bytes,
    with_data,
)
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store
from services.db.models import Source
from services.db.session import get_sessionmaker
from services.global_figures import read
from services.global_figures.load import LoadRefused, load_latest, load_run
from services.global_figures.models import GlobalElectricityLoad, GlobalElectricityValue
from services.global_figures.testing import WHEN, seed
from tests.db_template import disposing, fresh_engine

YEARLY = "/v1/electricity-generation/yearly"


@pytest.fixture()
def session() -> Iterator[Session]:
    with disposing(fresh_engine()) as engine:
        factory = get_sessionmaker(engine)
        with factory() as s:
            yield s


def _count(session: Session, **where: Any) -> int:
    query = sa.select(sa.func.count()).select_from(GlobalElectricityValue)
    for key, value in where.items():
        query = query.where(getattr(GlobalElectricityValue, key) == value)
    return int(session.scalar(query) or 0)


def test_every_dataset_loads_with_its_provenance(session: Session, tmp_path: pathlib.Path) -> None:
    out = seed(session, tmp_path)
    loads = {(load.dataset, load.origin): load for load in read.current_loads(session)}
    assert set(loads) == {
        ("yearly", "api"),
        ("yearly", "file"),
        ("monthly", "api"),
        ("monthly_capacity", "api"),
    }
    licence = Registry().get(SOURCE_ID).licence_id
    for load in loads.values():
        assert load.source_id == SOURCE_ID and load.licence_id == licence and load.retrieved_at is not None
    values = session.scalars(sa.select(GlobalElectricityValue)).all()
    assert len(values) == out["run"]["rows_seen"]
    assert all(
        v.source_id == SOURCE_ID and v.source_url and v.retrieved_at and v.licence_id == licence
        for v in values
    )
    assert loads[("yearly", "file")].metrics == "capacity_gw,generation_twh,share_of_generation_pct"
    assert loads[("yearly", "api")].countries_by_year == {"2023": 5, "2024": 5, "2025": 4}
    source = session.get(Source, SOURCE_ID)
    assert source is not None and source.implemented is True


def test_loading_twice_changes_nothing(session: Session, tmp_path: pathlib.Path) -> None:
    out = seed(session, tmp_path)
    before = _count(session)
    report = load_latest(session, store=out["store"])
    assert all(run["loaded"] == [] for run in report["runs"]) and _count(session) == before


def test_a_new_run_replaces_its_dataset_whole(session: Session, tmp_path: pathlib.Path) -> None:
    out = seed(session, tmp_path, inbox=False)
    store: Store = out["store"]
    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = with_data(
        YEARLY_JSON,
        lambda rows: [
            {**r, "generation_twh": 310.0}
            if (r["entity"], r["date"], r["series"]) == ("United States", "2025", "Solar")
            else r
            for r in rows
        ],
    )
    later = EmberRun(
        store,
        http=FakeSession(bodies),
        env={ENV_KEY: TEST_KEY},
        clock=fixed_clock(WHEN + dt.timedelta(days=14)),
    ).run()  # type: ignore[arg-type]
    first_ts = next(load.run_ts for load in read.current_loads(session))
    report = load_latest(session, store=store)
    loaded = [item for run in report["runs"] for item in run["loaded"]]
    assert [item["dataset"] for item in loaded] == ["yearly/api"]
    assert loaded[0]["replaced"] == [f"{first_ts}:yearly:api"]
    loads = read.current_loads(session)
    yearly = [load for load in loads if load.dataset == "yearly"]
    assert len(yearly) == 1 and yearly[0].run_ts == later.ts
    # The unchanged datasets keep the first run's load.
    assert {load.run_ts for load in loads if load.dataset != "yearly"} == {first_ts}
    us = read.yearly(session, yearly[0], "US", "generation_twh")
    assert us[2025]["solar"] == 310.0
    assert _count(session, load_id=f"{first_ts}:yearly:api") == 0
    assert _count(session, load_id=f"{later.ts}:yearly:api") == out["run"]["datasets"]["yearly/api"]["rows"]


def test_a_failed_download_and_a_failed_load_keep_the_figures(
    session: Session, tmp_path: pathlib.Path
) -> None:
    out = seed(session, tmp_path, inbox=False)
    store: Store = out["store"]
    before = {load.id for load in read.current_loads(session)}
    failed = EmberRun(
        store,
        http=FakeSession(statuses={YEARLY: 500}),
        env={ENV_KEY: TEST_KEY},
        clock=fixed_clock(WHEN + dt.timedelta(days=14)),
    ).run()  # type: ignore[arg-type]
    assert failed.record["status"] == "failed"
    load_latest(session, store=store)
    assert {load.id for load in read.current_loads(session)} == before


def test_a_load_that_dies_part_way_keeps_the_previous_figures(
    session: Session, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.global_figures.load as loader

    out = seed(session, tmp_path, inbox=False)
    session.commit()
    store: Store = out["store"]
    before, count = {load.id for load in read.current_loads(session)}, _count(session)
    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = with_data(
        YEARLY_JSON, lambda rows: [{**r, "generation_twh": r["generation_twh"] + 1} for r in rows]
    )
    EmberRun(
        store,
        http=FakeSession(bodies),
        env={ENV_KEY: TEST_KEY},
        clock=fixed_clock(WHEN + dt.timedelta(days=14)),
    ).run()  # type: ignore[arg-type]

    def dies(frame: Any, load_id: str) -> Iterator[list[dict[str, Any]]]:
        yield next(iter(original(frame, load_id)))
        raise RuntimeError("the database went away mid-load")

    original = loader._value_rows
    monkeypatch.setattr(loader, "_value_rows", dies)
    with pytest.raises(RuntimeError, match="mid-load"):
        load_latest(session, store=store)
    session.rollback()  # what `session_scope` does when the load raises
    assert {load.id for load in read.current_loads(session)} == before and _count(session) == count


def test_a_run_fetched_under_other_terms_is_refused(session: Session, tmp_path: pathlib.Path) -> None:
    out = seed(session, tmp_path, inbox=False)
    store: Store = out["store"]
    assert out["run"]["status"] == "ok"
    path = next((tmp_path / "normalized" / SOURCE_ID).iterdir())
    df = pd.read_parquet(path)
    df["licence_id"] = "global.ember.electricity#ffffffffff"
    df.to_parquet(path, index=False)
    session.execute(sa.delete(GlobalElectricityValue))
    session.execute(sa.delete(GlobalElectricityLoad))
    with pytest.raises(LoadRefused, match="terms changed"):
        load_run(session, path.stem, store=store)


def test_the_reader_follows_its_definitions(session: Session, tmp_path: pathlib.Path) -> None:
    seed(session, tmp_path)
    loads = read.current_loads(session)
    gen_load = read.pick(loads, "yearly", "generation_twh")
    assert gen_load is not None and gen_load.origin == "api"  # fetched after the person's download
    cap_load = read.pick(loads, "yearly", "capacity_gw")
    assert cap_load is not None and cap_load.origin == "file"  # the API serves no yearly capacity
    world = read.yearly(session, gen_load, "world", "generation_twh")
    block = read.generation_block(world, gen_load)
    assert block is not None and block["year"] == 2025 and block["previous_year"] == 2024
    solar = next(r for r in block["by_source"] if r["source"] == "solar")
    assert solar["generation_twh"] == 2700.0 and solar["change_pct"] == 30.0
    clean = read.clean_block(world)
    assert clean is not None and clean["definition"] == [
        "solar",
        "wind",
        "hydro",
        "nuclear",
        "other_renewables",
        "bioenergy",
    ]
    expected = (
        (2700 + 2800 + 4400 + 2750 + 100 + 700)
        / (2700 + 2800 + 4400 + 2750 + 6700 + 10400 + 100 + 700 + 650)
        * 100
    )
    assert clean["pct"] == round(expected, 2)
    coverage = read.coverage(gen_load)
    assert coverage is not None
    assert coverage["latest_year"] == 2025 and coverage["latest_complete_year"] == 2024
    assert coverage["countries_latest_year"] == 4 and coverage["countries_latest_complete_year"] == 5
    ranking = read.top_wind_solar_additions(session, loads)
    assert ranking is not None and ranking["basis"] == "yearly"
    # 2025 holds four of 2024's five countries (80% < 90%), so the ranking year is 2024 against 2023.
    assert ranking["year"] == 2024 and ranking["latest_capacity_year"] == 2025
    assert [row["area"]["id"] for row in ranking["countries"]] == ["CN", "US", "DE", "IN", "BR"]
    assert ranking["countries_compared"] == 5


def test_without_yearly_capacity_the_ranking_reads_december_against_december(
    session: Session, tmp_path: pathlib.Path
) -> None:
    seed(session, tmp_path, inbox=False)
    ranking = read.top_wind_solar_additions(session, read.current_loads(session))
    assert ranking is not None and ranking["basis"] == "monthly_december" and ranking["year"] == 2025
    assert "fewer countries" in ranking["note"]
    assert ranking["countries"][0]["area"]["id"] == "CN"


def test_complete_year_rule() -> None:
    assert read.complete_year({2023: 200, 2024: 210, 2025: 80}) == 2024
    assert read.complete_year({2024: 100, 2025: 95}) == 2025
    assert read.complete_year({2025: 10}) == 2025
    assert read.complete_year({}) is None


def test_area_lookup_reads_world_codes_and_region_names(session: Session, tmp_path: pathlib.Path) -> None:
    seed(session, tmp_path, inbox=False)
    known = read.areas(session, read.current_loads(session))
    assert known[0].id == "world"
    assert read.find_area(known, "WORLD").id == "world"  # type: ignore[union-attr]
    assert read.find_area(known, "us").id == "US"  # type: ignore[union-attr]
    assert read.find_area(known, "USA").id == "US"  # type: ignore[union-attr]
    assert read.find_area(known, "Europe").id == "europe"  # type: ignore[union-attr]
    assert read.find_area(known, "europe").kind == "region"  # type: ignore[union-attr]
    assert read.find_area(known, "Atlantis") is None and read.find_area(known, "") is None


def _memory_load(origin: str, last_period: str, retrieved: dt.datetime) -> GlobalElectricityLoad:
    return GlobalElectricityLoad(
        id=f"t:yearly:{origin}",
        dataset="yearly",
        origin=origin,
        run_ts="t",
        metrics="generation_twh,share_of_generation_pct",
        rows=1,
        areas=1,
        last_period=last_period,
        countries_by_year={},
        source_id=SOURCE_ID,
        licence_id="global.ember.electricity#0123456789",
        source_url="https://api.ember-energy.org/v1/electricity-generation/yearly?start_date=2000",
        retrieved_at=retrieved,
    )


def test_a_figure_comes_from_the_newest_period_then_the_api_never_a_later_copy() -> None:
    api = _memory_load("api", "2025", dt.datetime(2026, 10, 8, tzinfo=dt.UTC))
    # An old download copied into the inbox later: a newer `retrieved_at`, the same latest year.
    copied = _memory_load("file", "2025", dt.datetime(2026, 10, 20, tzinfo=dt.UTC))
    assert read.pick([copied, api], "yearly", "generation_twh") is api
    newer = _memory_load("file", "2026", dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
    assert read.pick([api, newer], "yearly", "generation_twh") is newer
    assert read.pick([api], "yearly", "capacity_gw") is None


def test_a_change_compares_only_years_with_the_same_generation_types() -> None:
    load = _memory_load("api", "2025", dt.datetime(2026, 10, 8, tzinfo=dt.UTC))
    series: read.Series = {
        2025: {"solar": 10.0, "wind": 10.0, "coal": 80.0},
        2024: {"solar": 5.0, "coal": 80.0},
    }
    block = read.generation_block(series, load)
    assert block is not None and block["sources_differ"] == ["wind"]
    assert (
        block["total_twh"] == 100.0
        and block["total_change_twh"] is None
        and block["total_change_pct"] is None
    )
    solar = next(r for r in block["by_source"] if r["source"] == "solar")
    assert solar["change_twh"] == 5.0 and solar["share_change_pp"] is None  # same source: still compared
    clean = read.clean_block(series)
    assert clean is not None and clean["change_pp"] is None and clean["sources_differ"] == ["wind"]
    same: read.Series = {2025: {"solar": 10.0, "coal": 90.0}, 2024: {"solar": 5.0, "coal": 95.0}}
    both = read.generation_block(same, load)
    assert both is not None and both["sources_differ"] == [] and both["total_change_twh"] == 0.0
    assert read.clean_block(same)["change_pp"] == 5.0  # type: ignore[index]


def test_blocks_and_rows_after_the_latest_complete_year_are_estimated() -> None:
    load = _memory_load("api", "2025", dt.datetime(2026, 10, 8, tzinfo=dt.UTC))
    series: read.Series = {2024: {"solar": 5.0, "coal": 95.0}, 2025: {"solar": 10.0, "coal": 90.0}}
    assert read.generation_block(series, load, estimated_after=2024)["estimated"] is True  # type: ignore[index]
    assert read.clean_block(series, estimated_after=2024)["estimated"] is True  # type: ignore[index]
    assert read.capacity_block(series, load, estimated_after=2024)["estimated"] is True  # type: ignore[index]
    rows = read.series_rows(series, {}, (None, None), estimated_after=2024)
    assert [(r["year"], r["estimated"]) for r in rows] == [(2024, False), (2025, True)]
    assert read.generation_block(series, load)["estimated"] is False  # type: ignore[index]  # a country


def test_the_ranking_says_its_additions_are_net_and_solar_mixes_ac_and_dc(
    session: Session, tmp_path: pathlib.Path
) -> None:
    seed(session, tmp_path)
    ranking = read.top_wind_solar_additions(session, read.current_loads(session))
    assert ranking is not None and "Net" in ranking["note"] and "retirements subtract" in ranking["note"]
    assert "AC or DC" in ranking["note"]
