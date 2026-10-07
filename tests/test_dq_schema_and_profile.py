"""DQ gates from the 2026-09-30 data engineer audit:

* F5: a renamed or missing source column holds the run instead of publishing nulls and change
  events (declared key columns, the source's own header, and the `field_nulled` backstop);
* F6: an incremental source's row count is compared on like-for-like weekdays, so TED's
  publication week no longer holds it, while a real drop still does;
* F13: zero rows hold unless the connector may be empty, and a manifest's `dq_thresholds` reach
  the gates.

Every run here reads a recorded fixture (or a header-renamed copy of one); nothing is fetched.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pandas as pd
import pytest
import yaml

from conftest import FIXTURES, ROOT
from pipeline.connectors import runner as runner_module
from pipeline.connectors.base import RawSnapshot
from pipeline.connectors.dq import DEFAULT_THRESHOLDS, daily_profile, run_gates
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store

T0 = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(days=7)


# ------------------------------------------------------------------------------- F5: renames
def _csv_rename(body: bytes, old: str, new: str) -> bytes:
    bom = body.startswith(b"\xef\xbb\xbf")
    head, rest = body.decode("utf-8-sig").split("\n", 1)
    cols = head.split(",")
    assert old in cols, (old, cols)
    out = ",".join(new if c == old else c for c in cols) + "\n" + rest
    return ("﻿" if bom else "").encode() + out.encode()


def _xlsx_rename(body: bytes, old: str, new: str) -> bytes:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(body))
    hits = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows(max_row=30):
            for cell in row:
                if isinstance(cell.value, str) and cell.value.strip() == old:
                    cell.value, hits = new, hits + 1
    assert hits, old
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


SOURCES = {
    "us.eia.860m": (
        "eia860m_planned.xlsx",
        "https://www.eia.gov/electricity/data/eia860m/xls/x.xlsx",
        _xlsx_rename,
    ),
    "gb.neso.tec_register": ("neso_tec_register.csv", "https://api.neso.energy/x.csv", _csv_rename),
    "us.permits_dashboard": (
        "permits_dashboard_projects.csv",
        "https://data.permits.performance.gov/x.csv",
        _csv_rename,
    ),
}


def _snap(source_id: str, body: bytes, at: dt.datetime) -> RawSnapshot:
    name, url, _ = SOURCES[source_id]
    return RawSnapshot(
        content=body,
        content_type="application/octet-stream",
        url=url,
        retrieved_at=at,
        http_status=200,
        ext=name.rsplit(".", 1)[1],
    )


@pytest.mark.parametrize(
    ("source_id", "column"),
    [
        # each of these published wrong events or silent nulls on 2026-09-30 (audit F5)
        ("us.eia.860m", "Planned Operation Year"),
        ("us.eia.860m", "County"),
        ("gb.neso.tec_register", "Connection Site"),
        ("gb.neso.tec_register", "Stage"),
        ("us.permits_dashboard", "Project Status"),
        ("us.permits_dashboard", "Project Location County"),
    ],
)
def test_a_renamed_column_holds_and_publishes_nothing(tmp_path, source_id, column):
    name, _, rename = SOURCES[source_id]
    body = (FIXTURES / name).read_bytes()
    st = Store(tmp_path)
    base = run(source_id, store=st, raw=_snap(source_id, body, T0))
    assert base.status == "ok"

    renamed = run(source_id, store=st, raw=_snap(source_id, rename(body, column, f"{column} (renamed)"), T1))
    assert renamed.status == "partial", renamed.run.get("dq")
    assert any(r.startswith("schema_drift") for r in renamed.run["hold_reasons"]), renamed.run["hold_reasons"]
    assert "normalized" not in renamed.paths and "events" not in renamed.paths
    assert renamed.run["events_emitted"] == 0


def test_every_declared_key_column_of_the_tabular_fixtures_holds_when_renamed(tmp_path, registry):
    """The per-connector guard the audit asked for: rename each declared key column in turn."""
    for source_id, (name, _, rename) in SOURCES.items():
        body = (FIXTURES / name).read_bytes()
        cls = registry.connector_class(source_id)
        for column in cls.key_source_columns:
            st = Store(tmp_path / f"{source_id}-{abs(hash(column))}")
            assert run(source_id, store=st, raw=_snap(source_id, body, T0)).status == "ok"
            try:
                mutated = rename(body, column, f"{column} (renamed)")
            except AssertionError:
                pytest.fail(f"{source_id}: declared key column {column!r} is not in the fixture header")
            r = run(source_id, store=st, raw=_snap(source_id, mutated, T1))
            # held by the schema gate, or refused by the parser itself (an identity column): either
            # way nothing reaches normalized/ or events/
            assert r.status in ("partial", "failed"), (source_id, column)
            assert "normalized" not in r.paths and "events" not in r.paths, (source_id, column)


def test_a_declared_column_missing_on_a_first_run_holds(tmp_path):
    name, _, rename = SOURCES["us.permits_dashboard"]
    body = rename((FIXTURES / name).read_bytes(), "Project Status", "Status of Project")
    r = run("us.permits_dashboard", store=Store(tmp_path), raw=_snap("us.permits_dashboard", body, T0))
    assert r.status == "partial"
    assert any("declared key columns missing" in h for h in r.run["hold_reasons"])


def _frame(n: int, county: list[Any] | None = None, state: str = "filed") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "record_id": [f"s:{i}" for i in range(n)],
            "source_id": "s",
            "source_url": "u",
            "retrieved_at": "t",
            "licence_id": "l",
            "name_canonical": "x",
            "capacity_mw": 10.0,
            "technology_raw": "Solar",
            "state": "TX",
            "county": county if county is not None else ["Travis"] * n,
            "lifecycle_state": state,
            "status_raw": "Active",
            "status_rule": "s.r",
        }
    )


def test_a_watched_field_nulled_on_re_fetched_rows_holds():
    prev = _frame(20)
    cur = _frame(20, county=[None] * 10 + ["Travis"] * 10)
    res = run_gates(cur, "proposal", ["a"], [], previous=prev, fetched=cur)
    assert res.held and any(c.check == "field_nulled:county" for c in res.checks)

    unknown = _frame(20, state="unknown")
    res = run_gates(unknown, "proposal", ["a"], [], previous=prev, fetched=unknown)
    assert any(c.check == "field_nulled:lifecycle_state" and c.level == "hold" for c in res.checks)

    # a field that was never set is not "lost"; two rows of twenty are under the threshold
    res = run_gates(
        _frame(20, county=[None] * 2 + ["Travis"] * 18),
        "proposal",
        ["a"],
        [],
        previous=prev,
        fetched=_frame(20, county=[None] * 2 + ["Travis"] * 18),
    )
    assert not any(c.level == "hold" for c in res.checks)


# ------------------------------------------------------------------------- F6: weekday profile
def _ted_like_runs(drop_from: dt.date | None = None) -> tuple[list[str], int]:
    """28 daily 03:07 runs of a source publishing 230 notices per business day (the 2026-09-13 TED
    snapshot), each window anchored a day before the previous run (`Connector.fetch_window`)."""
    history: list[dict[str, Any]] = []
    held, log = 0, []
    prev_run: dt.datetime | None = None
    d0 = dt.datetime(2026, 10, 1, 3, 7, tzinfo=dt.UTC)
    for i in range(28):
        now = d0 + dt.timedelta(days=i)
        start = (
            now - dt.timedelta(days=3)
            if prev_run is None
            else dt.datetime.combine((prev_run - dt.timedelta(days=1)).date(), dt.time(0), dt.UTC)
        )
        dates = []
        day = start.date()
        while day <= now.date():
            if day.weekday() < 5 and not (drop_from and day >= drop_from):
                dates += [pd.Timestamp(day, tz="UTC")] * 230
            day += dt.timedelta(days=1)
        counts = daily_profile(pd.Series(dates, dtype="datetime64[ns, UTC]"), start, now)
        n = len(dates)
        df = _frame(n)
        res = run_gates(df, "proposal", ["a"], history, rows_fetched=n, daily_counts=counts, incremental=True)
        row = next(c for c in res.checks if c.check == "row_count_drift")
        log.append(f"{now:%a}:{row.level}")
        if res.held:
            held += 1
        else:
            history.append(res.stats)
            prev_run = now
    return log, held


def test_a_publication_week_no_longer_holds_an_incremental_source():
    log, held = _ted_like_runs()
    assert held == 0, log


def test_a_real_drop_is_still_held():
    log, held = _ted_like_runs(drop_from=dt.date(2026, 10, 19))
    assert held >= 1, log
    assert all(entry.endswith(("pass", "info", "warn")) for entry in log[:18]), log


def test_the_previous_run_comparison_held_ted_four_days_in_seven():
    """The old gate, kept for full-register sources, on the same series: 16 of 28 held (audit F6)."""
    history: list[dict[str, Any]] = []
    held = 0
    for i in range(28):
        day = dt.date(2026, 10, 1) + dt.timedelta(days=i)
        n = sum(230 for k in (1, 2, 3) if (day - dt.timedelta(days=k)).weekday() < 5)
        res = run_gates(_frame(n), "proposal", ["a"], history, rows_fetched=n)
        if res.held:
            held += 1
        else:
            history.append(res.stats)
    assert held == 16


def test_daily_profile_counts_only_complete_days():
    start = dt.datetime(2026, 10, 5, 3, 7, tzinfo=dt.UTC)  # Monday, partial
    end = dt.datetime(2026, 10, 8, 3, 7, tzinfo=dt.UTC)  # Thursday, partial
    dates = pd.Series(pd.to_datetime(["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-07"], utc=True))
    assert daily_profile(dates, start, end) == {"2026-10-06": 1, "2026-10-07": 2}
    midnight = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)
    assert "2026-10-05" in daily_profile(dates, midnight, end)


# ------------------------------------------------------------------------------- F13
def test_zero_rows_hold_unless_the_source_may_be_empty():
    empty = _frame(0)
    assert run_gates(empty, "proposal", [], []).held
    assert not run_gates(empty, "proposal", [], [], may_be_empty=True).held
    # a short window of an incremental source can be empty over a weekend; a week cannot
    assert not run_gates(empty, "proposal", [], [], incremental=True, window_days=2.0).held
    assert run_gates(empty, "proposal", [], [], incremental=True, window_days=8.0).held


def test_the_ercot_large_load_watcher_may_be_empty(registry):
    assert registry.connector_class("us.iso.ercot.large_load_queue").may_be_empty


def test_manifest_dq_thresholds_reach_the_gates(tmp_path, monkeypatch, registry):
    seen: dict[str, Any] = {}
    real = runner_module.run_gates

    def spy(*args: Any, **kwargs: Any):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_module, "run_gates", spy)
    entry = registry.get("gb.neso.tec_register")
    monkeypatch.setitem(entry.raw, "dq_thresholds", {"row_drift_hold_pct": 90.0})
    name, _, _ = SOURCES["gb.neso.tec_register"]
    run(
        "gb.neso.tec_register",
        registry=registry,
        store=Store(tmp_path),
        raw=_snap("gb.neso.tec_register", (FIXTURES / name).read_bytes(), T0),
    )
    assert seen["thresholds"] == {"row_drift_hold_pct": 90.0}


def test_manifest_dq_threshold_keys_are_known():
    doc = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text())
    for s in doc["sources"]:
        for key in s.get("dq_thresholds") or {}:
            assert key in DEFAULT_THRESHOLDS, (s["id"], key)
