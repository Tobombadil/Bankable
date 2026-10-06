"""A failed load's change events arrive with the next load, exactly once (architect audit
2026-09-30 A6; `infra/scheduler/jobs.py::load_source_job`, `services/ingest/loader.py::runs_to_load`).

The runner diffs each run against the previous promoted snapshot, loaded or not. Before this
change, a load that failed (a timeout, a database error, a concurrent writer) dropped its run's
events for good: the next run's diff started from the failed run's state, and the scheduler loaded
only the next run. Here three runs are written exactly as the runner writes them
(`pipeline.connectors.runner._diff_and_store`, the same step a fetch and a hold release use), the
second load fails part-way through its events, and the third load must bring both runs' events.
"""

from __future__ import annotations

import datetime as dt
import functools
import pathlib
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from infra.scheduler import jobs
from pipeline.connectors.registry import Registry, SourceEntry
from pipeline.connectors.runner import RunResult, _diff_and_store
from pipeline.connectors.store import Store
from services.db.models import Event, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader

SOURCE_ID = "us.test.replay_queue"
TS = ("20260901T050000Z", "20260902T050000Z", "20260903T050000Z")


def _registry() -> Registry:
    entry = SourceEntry.from_yaml(
        {
            "id": SOURCE_ID,
            "name": "Replay Test Queue",
            "jurisdiction": "US-TX",
            "category": "generation_queue",
            "operator": "Test ISO",
            "url": "https://example.org/queue",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "daily",
            "tier": 1,
            "license": "US federal public domain",
        }
    )
    registry = Registry.__new__(Registry)
    registry.path = None  # type: ignore[assignment]
    registry.version = "2026-09-12"
    registry.sources = {entry.id: entry}
    return registry


def _row(queue_id: str, ts: str, *, state: str, mw: float) -> dict[str, Any]:
    retrieved = dt.datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC).isoformat()
    return {
        "record_id": f"{SOURCE_ID}:{queue_id}",
        "source_id": SOURCE_ID,
        "source_record_id": queue_id,
        "source_url": f"https://example.org/queue/{queue_id}",
        "retrieved_at": retrieved,
        "licence_id": "irrelevant",
        "kind": "storage",
        "name_canonical": f"Replay Storage {queue_id}",
        "name_norm": f"replay storage {queue_id.lower()}",
        "sponsor_name": "Acme Power LLC",
        "sponsor_norm": "acme power",
        "technology": "bess_li_ion",
        "technology_raw": "Battery Storage",
        "capacity_mw": mw,
        "storage_mwh": None,
        "iso": "ERCOT",
        "state": "TX",
        "county": "Travis",
        "county_norm": "travis",
        "lifecycle_state": state,
        "status_raw": state.upper(),
        "status_rule": "r1",
        "status_conflict": False,
        "queue_date": "2026-01-01",
        "proposed_cod": "2028-06-30",
        "queue_id": queue_id,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "cross_refs": None,
        "raw": f'{{"Queue ID": "{queue_id}"}}',
    }


#: Run 1 sees two projects; run 2 moves both forward (the events the failed load would lose);
#: run 3 changes one capacity.
FRAMES = (
    [_row("Q1", TS[0], state="filed", mw=100.0), _row("Q2", TS[0], state="filed", mw=50.0)],
    [_row("Q1", TS[1], state="studied", mw=100.0), _row("Q2", TS[1], state="withdrawn", mw=50.0)],
    [_row("Q1", TS[2], state="studied", mw=120.0), _row("Q2", TS[2], state="withdrawn", mw=50.0)],
)


def _write_runs(store: Store) -> None:
    prev: pd.DataFrame | None = None
    for i, (ts, rows) in enumerate(zip(TS, FRAMES, strict=True)):
        df = pd.DataFrame(rows)
        record: dict[str, Any] = {
            "id": f"00000000-0000-4000-8000-00000000000{i}",
            "status": "ok",
            "outputs": {},
        }
        _diff_and_store(
            store, SOURCE_ID, ts, df, prev, rows[0]["retrieved_at"], record, RunResult(run=record)
        )
        store.write_run(SOURCE_ID, ts, record)
        prev = df


@pytest.fixture()
def world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = get_sessionmaker(engine)
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    store = Store(tmp_path)
    _write_runs(store)
    load = functools.partial(loader.load_from_files, registry=_registry(), kind="proposal")
    return {"factory": factory, "root": tmp_path, "load": load}


def _job(world: dict[str, Any], ts: str) -> dict[str, Any]:
    return jobs.load_source_job(SOURCE_ID, ts, _load=world["load"], _data_root=world["root"])


def _changes(factory: sessionmaker[Session]) -> list[tuple[str, Any, Any]]:
    with factory() as s:
        rows = s.scalars(select(Event).where(Event.source_id == SOURCE_ID).order_by(Event.seq)).all()
        return [(e.event_type, e.before, e.after) for e in rows]


def _state(factory: sessionmaker[Session]) -> dict[str, tuple[str, float]]:
    with factory() as s:
        rows = s.execute(
            select(ProposalSource.source_record_id, Proposal.lifecycle_state, Proposal.capacity_mw).join(
                Proposal, Proposal.id == ProposalSource.proposal_id
            )
        ).all()
        return {qid: (state, float(mw)) for qid, state, mw in rows}


def _fail_after_first_event(monkeypatch: pytest.MonkeyPatch) -> None:
    real = loader._load_one_event
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database connection lost mid-load")
        return real(*args, **kwargs)

    monkeypatch.setattr(loader, "_load_one_event", flaky)


def test_a_failed_loads_events_arrive_with_the_next_load_exactly_once(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = world["factory"]
    _job(world, TS[0])
    assert _changes(factory) == []  # the first run of a source has no diff
    with factory() as s:
        source = s.get(Source, SOURCE_ID)
        assert source is not None and source.last_loaded_ts == TS[0]

    with monkeypatch.context() as m:
        _fail_after_first_event(m)
        with pytest.raises(RuntimeError, match="mid-load"):
            _job(world, TS[1])
    assert _changes(factory) == []  # rolled back: nothing of run 2 is stored
    assert _state(factory) == {"Q1": ("filed", 100.0), "Q2": ("filed", 50.0)}

    report = _job(world, TS[2])
    assert report["replayed"] == [TS[1]]
    changes = _changes(factory)
    assert changes == [
        # run 2, replayed: the events the failed load would have lost
        ("status_change", {"lifecycle_state": "filed"}, {"lifecycle_state": "studied"}),
        ("withdrawn", {"lifecycle_state": "filed"}, {"lifecycle_state": "withdrawn"}),
        # run 3
        ("capacity_changed", {"capacity_mw": "100.0"}, {"capacity_mw": "120.0"}),
    ]
    assert _state(factory) == {"Q1": ("studied", 120.0), "Q2": ("withdrawn", 50.0)}
    with factory() as s:
        source = s.get(Source, SOURCE_ID)
        assert source is not None and source.last_loaded_ts == TS[2]

    # Exactly once: the same load again, and the failed run's own load arriving late, add nothing.
    _job(world, TS[2])
    assert _job(world, TS[1]) == {"source_id": SOURCE_ID, "ts": TS[1], "skipped": "superseded"}
    assert _changes(factory) == changes


def test_a_replay_that_fails_part_way_keeps_the_runs_it_finished(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each replayed run commits on its own, so a long backlog makes progress across attempts."""
    factory = world["factory"]
    _job(world, TS[0])
    with monkeypatch.context() as m:
        _fail_after_first_event(m)
        with pytest.raises(RuntimeError):
            _job(world, TS[1])

    def fails_on_run3(session: Session, source_id: str, ts: str, **kw: Any) -> Any:
        if ts == TS[2]:
            raise RuntimeError("load timeout")
        return world["load"](session, source_id, ts, **kw)

    with pytest.raises(RuntimeError, match="timeout"):
        jobs.load_source_job(SOURCE_ID, TS[2], _load=fails_on_run3, _data_root=world["root"])
    with factory() as s:
        source = s.get(Source, SOURCE_ID)
        assert source is not None and source.last_loaded_ts == TS[1]  # run 2 replayed and kept
    assert len(_changes(factory)) == 2

    report = _job(world, TS[2])
    assert "replayed" not in report
    assert len(_changes(factory)) == 3


def test_runs_to_load_without_a_marker_loads_only_the_run_asked_for(world: dict[str, Any]) -> None:
    """A source loaded before migration 0032 has no marker; nothing is guessed."""
    with world["factory"]() as s:
        assert loader.runs_to_load(s, Store(world["root"]), SOURCE_ID, TS[2]) == [TS[2]]
