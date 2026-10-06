"""`services/ingest/retirements.py` end to end: the recorded August 2026 trim is the plant assets'
first load and the retirements connector's first run, then one edited month moves four planned
retirements. Runs go through the real runner into a temporary store, and loads through
`load_from_files`, the path the scheduler's `load_source` job takes."""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conftest import fixture_path, snapshot
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.connectors.us_eia_860m_retirements.test_connector import (
    FIXTURE,
    MONTH1,
    MONTH2,
    URL,
    month_two,
    raw_of,
)
from pipeline.context.eia_plants import aggregate_plants
from pipeline.context.retirements import parse_generator_sheets
from services.db.models import Asset, Event
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader
from services.ingest.plants import load_plants
from services.ingest.retirements import RetirementLoadResult, classify

SOURCE_ID = "us.eia.860m.retirements"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> Store:
    return Store(tmp_path)


def _load_plants(session: Session) -> None:
    sheets = parse_generator_sheets(fixture_path(FIXTURE).read_bytes())
    frame = aggregate_plants(
        sheets.operating, retrieved_at="2026-09-27T15:15:28Z", retired=sheets.retired, as_of=sheets.as_of
    )
    load_plants(session, frame)


def _run_and_load(session: Session, store: Store, raw: object) -> RetirementLoadResult:
    result = run(SOURCE_ID, registry=Registry(), store=store, raw=raw)  # type: ignore[arg-type]
    assert result.status == "ok", result.run.get("error")
    ts = result.paths["normalized"].stem
    loaded = loader.load_from_files(session, SOURCE_ID, ts, store=store, registry=Registry())
    session.commit()
    assert isinstance(loaded, RetirementLoadResult)
    return loaded


def _asset(session: Session, plant_id: str) -> Asset:
    asset = session.scalar(select(Asset).where(Asset.source_asset_id == plant_id))
    assert asset is not None
    return asset


def _events(session: Session) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.subject_type == "asset").order_by(Event.seq)))


def test_first_load_floods_nothing(session: Session, store: Store) -> None:
    _load_plants(session)
    first = _run_and_load(session, store, snapshot(FIXTURE, URL, retrieved_at=MONTH1))
    assert first.first_load is True
    assert first.events_created == {}
    assert _events(session) == []
    # The context load already wrote the same answer, so the first retirement load changes nothing.
    assert first.assets_updated == 0 and first.assets_unchanged == first.plants == 9
    assert first.plants_without_asset == 0
    statuses = dict(session.execute(select(Asset.source_asset_id, Asset.status)).all())
    assert (
        statuses["6155"] == "retired" and statuses["6166"] == "retiring" and statuses["2828"] == "operating"
    )


def test_a_moved_planned_retirement_is_an_event(session: Session, store: Store) -> None:
    _load_plants(session)
    _run_and_load(session, store, snapshot(FIXTURE, URL, retrieved_at=MONTH1))
    second = _run_and_load(session, store, raw_of(month_two(), MONTH2))

    events = {(session.get(Asset, e.subject_id).source_asset_id, e.event_type): e for e in _events(session)}  # type: ignore[union-attr]
    assert set(events) == {
        ("2828", "retirement_date_changed"),
        ("1", "retirement_planned"),
        ("6166", "retired"),
        ("1241", "retirement_cancelled"),
    }
    assert second.events_created == {
        "retirement_date_changed": 1,
        "retirement_planned": 1,
        "retired": 1,
        "retirement_cancelled": 1,
    }
    moved = events[("2828", "retirement_date_changed")]
    assert moved.after is not None and moved.after["units"] == [
        {
            "generator_id": "3",
            "technology": "Conventional Steam Coal",
            "capacity_mw": 650.0,
            "date_before": "2028-12",
            "date_after": "2030-06",
        }
    ]
    assert moved.reason and "2028-12 -> 2030-06" in moved.reason
    assert moved.source_id == SOURCE_ID and moved.licence_id and moved.source_url == URL
    assert moved.observed_at.replace(tzinfo=dt.UTC) == MONTH2
    assert moved.public_at is not None and moved.run_id is not None

    # The assets moved with the events.
    cardinal = _asset(session, "2828")
    assert cardinal.status == "operating" and cardinal.retirement_year == 2030
    assert cardinal.attributes["retirement"]["next_planned"] == "2030-06"
    rockport = _asset(session, "6166")
    assert rockport.status == "retiring"  # unit 2 still scheduled, and it is all that is left
    assert rockport.attributes["retirement"]["retired_units"] == 1
    sand_point = _asset(session, "1")
    assert sand_point.status == "operating" and sand_point.retirement_year == 2027  # 0.9 of 3.7 MW
    la_cygne = _asset(session, "1241")
    assert la_cygne.status == "retiring" and la_cygne.attributes["retirement"]["retiring_units"] == 1


def test_reloading_a_run_writes_nothing_twice(session: Session, store: Store) -> None:
    _load_plants(session)
    _run_and_load(session, store, snapshot(FIXTURE, URL, retrieved_at=MONTH1))
    result = run(SOURCE_ID, registry=Registry(), store=store, raw=raw_of(month_two(), MONTH2))
    ts = result.paths["normalized"].stem
    loader.load_from_files(session, SOURCE_ID, ts, store=store, registry=Registry())
    session.commit()
    before = len(_events(session))
    again = loader.load_from_files(session, SOURCE_ID, ts, store=store, registry=Registry())
    session.commit()
    assert isinstance(again, RetirementLoadResult)
    assert len(_events(session)) == before == 4
    assert again.events_skipped_idempotent == 4 and again.assets_updated == 0


def test_a_plant_without_an_asset_is_counted_not_created(session: Session, store: Store) -> None:
    first = _run_and_load(session, store, snapshot(FIXTURE, URL, retrieved_at=MONTH1))
    assert first.plants_without_asset == 9
    assert session.scalar(select(func.count()).select_from(Asset)) == 0


@pytest.mark.parametrize(
    ("rows", "unit", "expected"),
    [
        (
            [{"event_type": "status_change", "before": "operating", "after": "standby"}],
            {"state": "standby"},
            None,
        ),
        ([{"event_type": "capacity_change", "before": "10", "after": "12"}], {"state": "retiring"}, None),
        (
            [{"event_type": "cod_change", "before": None, "after": "2030-01-01"}],
            {"state": "retiring", "date": "2030"},
            None,
        ),
        ([{"event_type": "new"}], {"state": "retired", "date": "2011-05"}, None),
        ([{"event_type": "new"}], {"state": "retired", "date": "2026-02"}, ("retired", None, "2026-02")),
        ([{"event_type": "new"}], {"state": "operating", "date": None}, None),
        (
            [{"event_type": "status_change", "before": "retired", "after": "operating"}],
            {"state": "operating", "date": None},
            ("returned_to_service", None, None),
        ),
    ],
)
def test_only_retirement_news_becomes_an_event(
    rows: list[dict[str, object]], unit: dict[str, object], expected: object
) -> None:
    assert classify(rows, unit, 2026) == expected
