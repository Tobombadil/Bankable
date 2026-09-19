"""A corrupt download must fail closed *inside* the runner (audit 2026-09-18 §3.1, item 3): the
run is recorded `failed` with the error, and the next source in a batch still runs.

Before this test the runner only caught `ConnectorError`/`KeyError`/`ValueError`/`TypeError`
around parse/normalise, so an `openpyxl`/`zipfile` error from a truncated workbook escaped,
left no run record and aborted `python -m pipeline.connectors run --all`.
"""

from __future__ import annotations

import datetime as dt
import json
import zipfile
from typing import Any, ClassVar

import pandas as pd
import pytest

from pipeline.connectors import __main__ as cli
from pipeline.connectors.base import Connector, RawSnapshot
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store

BROKEN = "us.iso.caiso.gen_queue"
HEALTHY = "us.iso.ercot.gen_queue"
WHEN = dt.datetime(2026, 9, 12, 6, 0, tzinfo=dt.UTC)


def _snap(source_id: str, content: bytes = b"garbage") -> RawSnapshot:
    return RawSnapshot(
        content=content,
        content_type="application/octet-stream",
        url=f"https://example.invalid/{source_id}",
        retrieved_at=WHEN,
        http_status=200,
        ext="xlsx",
    )


class Corrupt(Connector):
    kind: ClassVar[Any] = "proposal"
    ext: ClassVar[str] = "xlsx"

    def fetch(self) -> RawSnapshot:
        return _snap(self.source_id)

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        raise zipfile.BadZipFile("File is not a zip file")  # what openpyxl raises on a truncated xlsx

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        raise AssertionError("never reached")


class Healthy(Connector):
    kind: ClassVar[Any] = "proposal"
    ext: ClassVar[str] = "csv"

    def fetch(self) -> RawSnapshot:
        return _snap(self.source_id, b"ok")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        return [{"Queue ID": "Q1", "Status": "Active"}]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "source_record_id": ["Q1"],
                "lifecycle_state": ["studied"],
                "status_raw": ["Active"],
                "status_rule": ["test"],
                "capacity_mw": [1.0],
                "name_canonical": ["x"],
            }
        )
        return self.finalize(df, rows, raw)


@pytest.fixture()
def patched_registry(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> Registry:
    def instantiate(source_id: str, **kw: Any) -> Connector:
        cls = Corrupt if source_id == BROKEN else Healthy
        cls.source_id = source_id
        return cls(registry.get(source_id))

    monkeypatch.setattr(registry, "instantiate", instantiate)
    return registry


def test_a_parse_crash_is_recorded_as_a_failed_run(tmp_path, patched_registry: Registry) -> None:
    store = Store(tmp_path)
    res = run(BROKEN, registry=patched_registry, store=store)
    assert res.status == "failed"
    assert res.run["error_class"] == "BadZipFile"
    assert "not a zip file" in str(res.run["error"])
    record = json.loads(res.paths["run"].read_text())
    assert record["status"] == "failed" and record["error_class"] == "BadZipFile"
    # the raw bytes are kept as evidence, nothing publishable is written
    assert res.paths["snapshot"].exists()
    assert "normalized" not in res.paths and "events" not in res.paths


def test_the_batch_continues_past_a_broken_source(tmp_path, patched_registry: Registry, monkeypatch) -> None:
    monkeypatch.setattr(cli, "Registry", lambda: patched_registry)
    rc = cli.main(["run", BROKEN, HEALTHY, "--data-dir", str(tmp_path)])
    assert rc == 1  # a failed source is reported...
    store = Store(tmp_path)
    assert [r["status"] for r in store.runs(BROKEN)] == ["failed"]
    assert [r["status"] for r in store.runs(HEALTHY)] == ["ok"]  # ...and the next one still ran


def test_an_exception_escaping_the_runner_itself_does_not_abort_the_batch(
    tmp_path, patched_registry: Registry, monkeypatch
) -> None:
    """Defence in depth for the CLI loop: even a failure the runner cannot record (here: the store
    refusing the run-record write) is logged per source and the loop moves on."""
    monkeypatch.setattr(cli, "Registry", lambda: patched_registry)

    def boom(*a: Any, **kw: Any) -> Any:
        raise OSError("disk full")

    calls: list[str] = []
    real_run = cli.run

    def flaky_run(source_id: str, **kw: Any) -> Any:
        calls.append(source_id)
        if source_id == BROKEN:
            boom()
        return real_run(source_id, **kw)

    monkeypatch.setattr(cli, "run", flaky_run)
    rc = cli.main(["run", BROKEN, HEALTHY, "--data-dir", str(tmp_path)])
    assert calls == [BROKEN, HEALTHY]
    assert rc == 1
