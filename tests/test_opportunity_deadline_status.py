"""An `open` opportunity whose deadline has passed is `closed` (docs/21 §7.2), including rows an
incremental source carries forward without re-fetching them (audit 2026-09-30: data engineer F4,
market M-7, designer D-1; 90 of 405 stored `open` notices had a past `due_at`)."""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pandas as pd

from conftest import RECORDED_AT, snapshot
from pipeline.connectors.opportunity import DEADLINE_PASSED_RULE, close_past_deadline
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store

TED = "eu.ted.api"
TED_URL = "https://api.ted.europa.eu/v3/notices/search"
UTC = dt.UTC


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "record_id": ["a", "b", "c", "d", "e"],
            "status": ["open", "open", "open", "announced", "closed"],
            "status_rule": ["m", "m", "m", "m", "m"],
            "due_at": [
                pd.Timestamp("2026-09-14", tz="UTC"),
                pd.Timestamp("2026-12-01", tz="UTC"),
                pd.NaT,
                pd.Timestamp("2026-09-14", tz="UTC"),
                pd.Timestamp("2026-09-01", tz="UTC"),
            ],
        }
    )


def test_open_past_its_deadline_is_closed_and_nothing_else_moves() -> None:
    out, moved = close_past_deadline(_frame(), dt.datetime(2026, 9, 30, tzinfo=UTC))
    assert moved == 1
    assert list(out["status"]) == ["closed", "open", "open", "announced", "closed"]
    assert list(out["status_rule"]) == [DEADLINE_PASSED_RULE, "m", "m", "m", "m"]


def test_the_deadline_is_compared_in_utc_whatever_the_stored_form() -> None:
    df = _frame()
    df["due_at"] = ["2026-09-30T10:00:00Z", None, None, None, None]
    _, before = close_past_deadline(df, dt.datetime(2026, 9, 30, 9, 59, tzinfo=UTC))
    _, after = close_past_deadline(df, dt.datetime(2026, 9, 30, 10, 1, tzinfo=UTC))
    assert (before, after) == (0, 1)


def _ted_without(content: bytes, publication_number: str) -> bytes:
    doc = json.loads(content)
    for page in doc["pages"]:
        page["notices"] = [n for n in page["notices"] if n.get("publication-number") != publication_number]
    return json.dumps(doc).encode("utf-8")


def test_a_carried_forward_notice_closes_when_its_deadline_passes(tmp_path: pathlib.Path) -> None:
    """TED is incremental: notice 619343-2026 (open, due 2026-10-20) is fetched on 2026-09-12 and
    not returned again. On a run of 2026-12-11 it is carried forward, and must not stay open."""
    store, registry = Store(tmp_path), Registry()
    first = run(
        TED, registry=registry, store=store, raw=snapshot("ted_search.json", TED_URL, "application/json")
    )
    assert first.status == "ok", first.run.get("error")
    assert first.records is not None
    assert first.records.set_index("record_id").at["eu.ted.api:619343-2026", "status"] == "open"

    later = snapshot(
        "ted_search.json", TED_URL, "application/json", retrieved_at=dt.datetime(2026, 12, 11, tzinfo=UTC)
    )
    later.content = _ted_without(later.content, "619343-2026")
    second = run(TED, registry=registry, store=store, raw=later)
    assert second.status == "ok", second.run.get("error")
    assert second.records is not None
    row = second.records.set_index("record_id").loc["eu.ted.api:619343-2026"]
    assert (row["status"], row["status_rule"]) == ("closed", DEADLINE_PASSED_RULE)
    ev = second.events
    assert ev is not None
    moved = ev[ev["record_id"] == "eu.ted.api:619343-2026"]
    assert list(zip(moved["event_type"].astype(str), moved["before"], moved["after"], strict=True)) == [
        ("status_change", "open", "closed")
    ]
    assert second.dq is not None
    assert [c.data["rows"] for c in second.dq.checks if c.check == "deadline_closed"] == [1]
    assert RECORDED_AT < dt.datetime(2026, 10, 20, tzinfo=UTC)
