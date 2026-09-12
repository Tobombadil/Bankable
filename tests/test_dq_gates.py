"""Data-quality gate tests (docs/04 DA-6 thresholds, docs/20 §12).

Each check is exercised at its warn and hold thresholds, and a held run is proved to write
nothing publishable.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from pipeline.connectors.base import Connector, RawSnapshot
from pipeline.connectors.dq import DEFAULT_THRESHOLDS, run_gates, snapshot_stats
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store

COLUMNS = ["Queue ID", "Project Name", "Status", "Capacity (MW)", "Generation Type", "State"]


def frame(
    n: int = 100,
    *,
    state: str = "studied",
    rule: str = "ercot.map",
    nulls: int = 0,
    duplicate: bool = False,
    missing_provenance: bool = False,
    status_raw: str = "Active",
) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "record_id": [f"s:{i}" for i in range(n)],
            "source_id": "s",
            "source_url": "https://example.invalid/x",
            "retrieved_at": "2026-09-12T00:00:00Z",
            "licence_id": "lic#1",
            "lifecycle_state": state,
            "status_raw": status_raw,
            "status_rule": rule,
            "name_canonical": [f"P{i}" for i in range(n)],
            "capacity_mw": [float(i + 1) for i in range(n)],
            "technology_raw": "Solar",
            "state": "TX",
            "kind": "generation",
        }
    )
    if nulls:
        df.loc[: nulls - 1, "capacity_mw"] = None
    if duplicate:
        df.loc[1, "record_id"] = df.loc[0, "record_id"]
    if missing_provenance:
        df.loc[0, "licence_id"] = None
    return df


def gates(df: pd.DataFrame, history: list[dict] | None = None, **kw):
    return run_gates(df, "proposal", COLUMNS, history or [], **kw)


def level_of(result, check: str) -> str:
    return next(c.level for c in result.checks if c.check == check)


# ---------------------------------------------------------------- row-count drift
def test_first_run_has_no_row_count_baseline():
    r = gates(frame(100))
    assert level_of(r, "row_count_drift") == "info"
    assert r.status == "pass"


@pytest.mark.parametrize(
    "rows,expected", [(100, "pass"), (95, "pass"), (89, "warn"), (115, "warn"), (69, "hold"), (131, "hold")]
)
def test_row_count_drift_thresholds(rows: int, expected: str):
    history = [snapshot_stats(frame(100), "proposal", COLUMNS) | {"rows_fetched": 100}]
    r = gates(frame(rows), history)
    assert level_of(r, "row_count_drift") == expected


def test_row_drift_defaults_match_the_standard():
    assert DEFAULT_THRESHOLDS["row_drift_warn_pct"] == 10.0
    assert DEFAULT_THRESHOLDS["row_drift_hold_pct"] == 30.0


# ---------------------------------------------------------------- vocabulary drift
def test_any_unmapped_status_warns():
    df = frame(100)
    df.loc[:1, "status_rule"] = "ercot.unmapped"
    df.loc[:1, "status_raw"] = "SOMETHING NEW"
    r = gates(df)
    assert level_of(r, "vocabulary_unmapped") == "warn"
    assert r.status == "warn"
    assert "SOMETHING NEW" in str(r.to_dict())


def test_unmapped_above_five_percent_holds():
    df = frame(100)
    df.loc[:9, "status_rule"] = "ercot.unmapped"
    r = gates(df)
    assert level_of(r, "vocabulary_unmapped") == "hold"
    assert r.held


def test_a_new_raw_status_value_warns_against_the_previous_run():
    history = [snapshot_stats(frame(100, status_raw="Active"), "proposal", COLUMNS)]
    r = gates(frame(100, status_raw="Suspended"), history)
    assert level_of(r, "vocabulary_drift") == "warn"


# ---------------------------------------------------------------- null spikes
@pytest.mark.parametrize("nulls,expected", [(0, "pass"), (4, "pass"), (6, "warn"), (12, "hold")])
def test_null_spike_thresholds(nulls: int, expected: str):
    history = [snapshot_stats(frame(100), "proposal", COLUMNS) for _ in range(5)]
    r = gates(frame(100, nulls=nulls), history)
    assert level_of(r, "null_rate:capacity_mw") == expected


def test_null_rate_uses_the_trailing_median_of_five_runs():
    history = [snapshot_stats(frame(100, nulls=n), "proposal", COLUMNS) for n in (20, 20, 20, 20, 20)]
    r = gates(frame(100, nulls=21), history)
    assert level_of(r, "null_rate:capacity_mw") == "pass"


# ---------------------------------------------------------------- duplicates and provenance
def test_duplicate_record_ids_hold():
    r = gates(frame(100, duplicate=True))
    assert level_of(r, "duplicate_keys") == "hold"
    assert r.held
    assert "duplicate_keys" in r.hold_reasons()[0]


def test_declared_suffixed_duplicates_only_warn():
    r = gates(frame(100), duplicates_resolved=2)
    assert level_of(r, "duplicate_keys") == "warn"


def test_a_row_missing_the_provenance_quartet_holds():
    r = gates(frame(100, missing_provenance=True))
    assert level_of(r, "provenance") == "hold"


# ---------------------------------------------------------------- schema drift
def test_a_new_source_column_warns():
    history = [snapshot_stats(frame(100), "proposal", COLUMNS)]
    r = run_gates(frame(100), "proposal", [*COLUMNS, "New Column"], history)
    assert level_of(r, "schema_drift") == "warn"


def test_removing_a_key_source_column_holds():
    history = [snapshot_stats(frame(100), "proposal", COLUMNS)]
    r = run_gates(frame(100), "proposal", COLUMNS[:-1], history, key_source_columns=("State",))
    assert level_of(r, "schema_drift") == "hold"


# ---------------------------------------------------------------- status mapping
def test_dq_status_maps_onto_the_source_run_vocabulary():
    assert gates(frame(100)).dq_status == "pass"
    assert gates(frame(100), duplicates_resolved=1).dq_status == "warn"
    assert gates(frame(100, duplicate=True)).dq_status == "fail"


# ---------------------------------------------------------------- a held run writes nothing
class _Holding(Connector):
    source_id = "us.iso.ercot.gen_queue"
    kind = "proposal"
    ext = "csv"

    def parse(self, raw: RawSnapshot) -> list[dict[str, object]]:
        return [{"Queue ID": "1"}, {"Queue ID": "1"}]

    def normalize(self, rows, raw):
        df = pd.DataFrame(
            {
                "source_record_id": ["1", "1"],
                "lifecycle_state": "studied",
                "status_raw": "Active",
                "status_rule": "ercot.map",
                "capacity_mw": [1.0, 2.0],
                "name_canonical": ["a", "b"],
                "technology_raw": "Solar",
                "state": "TX",
            }
        )
        return self.finalize(df, rows, raw)


def test_a_held_run_stores_the_snapshot_but_nothing_publishable(tmp_path, registry, monkeypatch):
    monkeypatch.setattr(registry, "connector_class", lambda sid: _Holding)
    raw = RawSnapshot(
        content=b"a,b\n1,2\n",
        content_type="text/csv",
        url="https://example.invalid/q.csv",
        retrieved_at=dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
        http_status=200,
        ext="csv",
    )
    result = run("us.iso.ercot.gen_queue", registry=registry, store=Store(tmp_path), raw=raw)
    assert result.status == "partial"
    assert result.run["dq_status"] == "fail"
    assert result.run["hold_reasons"]
    assert (tmp_path / "snapshots").exists()  # evidence is kept
    assert (tmp_path / "held").exists()  # output quarantined for the task queue
    assert not (tmp_path / "normalized").exists()  # nothing publishable
    assert not (tmp_path / "events").exists()  # and no `removed` events from a held run
