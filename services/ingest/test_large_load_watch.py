"""ERCOT's large-load catalogue watch never becomes a project on the public list (2026-10-07).

The connector emits one row per EMIL product that looks like the Protocol 3.2.7 status report.
Loaded by the generic loader, that row was a `kind = load` proposal named after the report, with
ERCOT as sponsor, and a `new` event for alerts. The specialised loader records the run and warns.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conftest import snapshot
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from services.db.models import Event, Proposal, SourceRun
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader

SOURCE_ID = "us.iso.ercot.large_load_queue"
URL = "https://www.ercot.com/api/1/services/read/common/filter-emil-items-search.json"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _run_and_load(session: Session, tmp_path: pathlib.Path, fixture: str) -> loader.LoadResult:
    store = Store(tmp_path)
    raw = snapshot(fixture, URL, "application/json")
    result = run(SOURCE_ID, registry=Registry(), store=store, raw=raw)
    assert result.status == "ok", result.run.get("error")
    ts = result.paths["normalized"].stem
    loaded = loader.load_from_files(session, SOURCE_ID, ts, store=store, registry=Registry())
    session.commit()
    assert isinstance(loaded, loader.LoadResult)
    return loaded


def test_a_catalogue_product_is_not_loaded_as_a_load_project(session, tmp_path):
    loaded = _run_and_load(session, tmp_path, "ercot_emil_catalogue_with_report.json")
    assert session.scalar(select(func.count()).select_from(Proposal)) == 0
    assert session.scalar(select(func.count()).select_from(Event)) == 0
    assert loaded.proposals_created == 0
    assert any("Large Load Interconnection Status Report" in w for w in loaded.warnings)
    # The run is still recorded, so the source's health and history stay right.
    assert (
        session.scalar(select(func.count()).select_from(SourceRun).where(SourceRun.source_id == SOURCE_ID))
        == 1
    )


def test_an_empty_watch_run_loads_quietly(session, tmp_path):
    loaded = _run_and_load(session, tmp_path, "ercot_emil_catalogue.json")
    assert loaded.warnings == [] and loaded.proposals_created == 0
    assert loader.kind_refusal(Registry(), SOURCE_ID) is None  # the scheduler still queues its loads
