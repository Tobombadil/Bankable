"""The run trigger and the data-quality hold release at the runner and CLI level (2026-09-27).

* `trigger`: the CLI records `manual` unless told otherwise, `--trigger schedule` is what the
  scheduler passes, and an off-vocabulary value is refused before any I/O.
* `release_held`: a run the DQ gates held (`partial`, frame under `held/`) is promoted to
  `normalized/` + `events/` by the same diff-and-store step a passing run uses, its record turns
  `ok` with a `release` block and becomes the next run's baseline. Idempotent; refuses a run that
  is not held, one a later run has superseded, and any gated source.

The API route and the scheduler job around it are tested in `tests/test_api_admin_sources.py` and
`infra/scheduler/test_loop.py`.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any, ClassVar

import pandas as pd
import pytest

from pipeline.connectors import __main__ as cli
from pipeline.connectors.base import Connector, RawSnapshot
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import RUN_TRIGGERS, ReleaseRefused, release_held, run
from pipeline.connectors.store import Store

SOURCE_ID = "us.iso.ercot.gen_queue"  # open, implemented
GATED_ID = "us.iso.pjm.gen_queue"  # restricted until a licence exists (CLAUDE.md)
T0 = dt.datetime(2026, 9, 1, 3, 7, tzinfo=dt.UTC)


class Sized(Connector):
    """A proposal connector whose frame has as many rows as the snapshot's first line says."""

    kind: ClassVar[Any] = "proposal"
    ext: ClassVar[str] = "csv"

    def fetch(self) -> RawSnapshot:
        return _snap(10, T0)

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        n, _, variant = raw.content.decode().partition(":")
        return [{"Queue ID": f"Q{i}", "Status": "Active", "Variant": variant} for i in range(int(n))]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "source_record_id": [r["Queue ID"] for r in rows],
                "lifecycle_state": "studied",
                "status_raw": "Active",
                "status_rule": "test.map",
                "capacity_mw": [
                    float(i + 1) + (0.5 if rows[0]["Variant"] else 0.0) for i in range(len(rows))
                ],
                "name_canonical": [f"Project {r['Queue ID']}" for r in rows],
                "technology_raw": "Solar",
                "state": "TX",
            }
        )
        return self.finalize(df, rows, raw)


def _snap(n: int, when: dt.datetime, variant: str = "") -> RawSnapshot:
    return RawSnapshot(
        content=f"{n}:{variant}".encode(),
        content_type="text/csv",
        url="https://example.invalid/queue.csv",
        retrieved_at=when,
        http_status=200,
        ext="csv",
    )


@pytest.fixture()
def sized(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> Registry:
    def connector_class(source_id: str) -> type[Connector]:
        Sized.source_id = source_id
        return Sized

    monkeypatch.setattr(registry, "connector_class", connector_class)
    return registry


def _run(registry: Registry, store: Store, n: int, day: int, variant: str = "") -> dict[str, Any]:
    when = T0 + dt.timedelta(days=day)
    return run(SOURCE_ID, registry=registry, store=store, raw=_snap(n, when, variant), trigger="schedule").run


def _held_after_a_baseline(registry: Registry, store: Store) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = _run(registry, store, 100, 0)
    held = _run(registry, store, 50, 1)  # -50 % rows: over the 30 % hold threshold
    assert baseline["status"] == "ok"
    assert held["status"] == "partial" and held["dq_status"] == "fail"
    return baseline, held


# ------------------------------------------------------------------------------------ trigger
def test_the_runner_refuses_an_off_vocabulary_trigger_before_any_io(
    tmp_path: pathlib.Path, sized: Registry
) -> None:
    with pytest.raises(ValueError, match="trigger"):
        run(SOURCE_ID, registry=sized, store=Store(tmp_path), raw=_snap(5, T0), trigger="scheduled")
    assert not any(tmp_path.iterdir())
    assert RUN_TRIGGERS == ("schedule", "manual", "backfill", "retry")


def _cli_trigger(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sized: Registry, *extra: str
) -> str:
    monkeypatch.setattr(cli, "Registry", lambda: sized)
    monkeypatch.setattr(Sized, "fetch", lambda self: _snap(5, T0 + dt.timedelta(days=len(extra))))
    assert cli.main(["run", SOURCE_ID, "--data-dir", str(tmp_path), *extra]) == 0
    record_path = max((tmp_path / "runs" / SOURCE_ID).glob("*.json"))
    return str(json.loads(record_path.read_text())["trigger"])


def test_a_cli_run_records_manual_by_default(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sized: Registry
) -> None:
    assert _cli_trigger(tmp_path, monkeypatch, sized) == "manual"


def test_the_cli_records_the_trigger_it_is_given(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sized: Registry
) -> None:
    assert _cli_trigger(tmp_path, monkeypatch, sized, "--trigger", "schedule") == "schedule"


def test_the_cli_rejects_an_unknown_trigger(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sized: Registry
) -> None:
    monkeypatch.setattr(cli, "Registry", lambda: sized)
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", SOURCE_ID, "--data-dir", str(tmp_path), "--trigger", "scheduled"])
    assert exc.value.code == 2


# ------------------------------------------------------------------------------- hold release
def test_a_held_run_is_released_into_normalized_and_becomes_the_baseline(
    tmp_path: pathlib.Path, sized: Registry
) -> None:
    store = Store(tmp_path)
    baseline, held = _held_after_a_baseline(sized, store)
    # The same bytes again: `unchanged`, because the held run's sha counts. Before the release
    # route, this is where a held source stayed until its data changed or someone edited rows.
    assert _run(sized, store, 50, 2)["status"] == "unchanged"
    assert store.previous_normalized(SOURCE_ID)[1] == baseline["id"]

    result = release_held(SOURCE_ID, held["id"], released_by="usr_TEST", registry=sized, store=store)

    assert not result.already_released
    record = json.loads(store.run_path(SOURCE_ID, result.ts).read_text())
    assert record["status"] == "ok"
    assert record["dq_status"] == "fail", "the gate's own verdict is kept; the release is recorded beside it"
    assert record["release"]["released_by"] == "usr_TEST" and record["release"]["released_at"]
    assert record["rows_gone"] == 50 and record["rows_new"] == 0
    assert record["events_emitted"] >= 50
    assert store.exists(store.normalized_path(SOURCE_ID, result.ts))
    assert store.exists(store.events_path(SOURCE_ID, result.ts))
    assert store.exists(store.held_path(SOURCE_ID, result.ts)), "the held frame stays as evidence"
    # The released frame is now what the next run diffs against and drifts from.
    frame, prev_id = store.previous_normalized(SOURCE_ID)
    assert prev_id == held["id"] and frame is not None and len(frame) == 50
    assert store.dq_history(SOURCE_ID)[-1]["rows"] == 50
    assert _run(sized, store, 50, 3, variant="b")["status"] == "ok"


def test_releasing_twice_is_idempotent(tmp_path: pathlib.Path, sized: Registry) -> None:
    store = Store(tmp_path)
    _, held = _held_after_a_baseline(sized, store)
    first = release_held(SOURCE_ID, held["id"], released_by="usr_A", registry=sized, store=store)
    body = store.run_path(SOURCE_ID, first.ts).read_bytes()
    second = release_held(SOURCE_ID, held["id"], released_by="usr_B", registry=sized, store=store)
    assert second.already_released and second.ts == first.ts
    assert store.run_path(SOURCE_ID, first.ts).read_bytes() == body, "nothing is rewritten"
    assert second.run["release"]["released_by"] == "usr_A"


def test_a_run_that_is_not_held_is_refused(tmp_path: pathlib.Path, sized: Registry) -> None:
    store = Store(tmp_path)
    baseline, _ = _held_after_a_baseline(sized, store)
    with pytest.raises(ReleaseRefused) as exc:
        release_held(SOURCE_ID, baseline["id"], released_by="usr_A", registry=sized, store=store)
    assert exc.value.code == "not_held"
    with pytest.raises(ReleaseRefused) as missing:
        release_held(SOURCE_ID, "no-such-run", released_by="usr_A", registry=sized, store=store)
    assert missing.value.code == "not_found"


def test_a_run_superseded_by_a_later_run_with_output_is_refused(
    tmp_path: pathlib.Path, sized: Registry
) -> None:
    store = Store(tmp_path)
    _, held = _held_after_a_baseline(sized, store)
    later = _run(sized, store, 100, 2, variant="c")  # back within the thresholds of the baseline
    assert later["status"] == "ok"
    with pytest.raises(ReleaseRefused) as exc:
        release_held(SOURCE_ID, held["id"], released_by="usr_A", registry=sized, store=store)
    assert exc.value.code == "superseded"
    assert store.previous_normalized(SOURCE_ID)[1] == later["id"], "the newer data is not rolled back"


def test_a_gated_source_is_never_released(tmp_path: pathlib.Path, registry: Registry) -> None:
    assert registry.get(GATED_ID).gated
    with pytest.raises(ReleaseRefused) as exc:
        release_held(GATED_ID, "any", released_by="usr_A", registry=registry, store=Store(tmp_path))
    assert exc.value.code == "gated"
    assert not any(tmp_path.iterdir()), "refused before any read or write"
