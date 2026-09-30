"""End-to-end run of the ERCOT connector over the recorded GIS Report fixture.

fetch → snapshot → parse → normalise → DQ gates → diff → store, with no network (docs/20 §3).
Running the connector twice proves the diff wiring: an unchanged payload short-circuits, and a
changed payload produces change events against the previous normalised snapshot.
"""

from __future__ import annotations

import datetime as dt
import io
import json

import openpyxl
import pandas as pd
import pytest

from conftest import fixture_path, snapshot
from pipeline.connectors.base import PROPOSAL_COLUMNS
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store

SOURCE_ID = "us.iso.ercot.gen_queue"
URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1269363208"
DAY1 = dt.datetime(2026, 9, 12, 6, 0, tzinfo=dt.UTC)
DAY2 = dt.datetime(2026, 9, 13, 6, 0, tzinfo=dt.UTC)


def edited_workbook(mutate) -> bytes:
    """The recorded GIS Report with one sheet edited, so day 2 differs from day 1."""
    wb = openpyxl.load_workbook(fixture_path("ercot_gis_report.xlsx"))
    mutate(wb["Project Details - Large Gen"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path)


@pytest.fixture()
def day1(registry, store):
    return run(
        SOURCE_ID,
        registry=registry,
        store=store,
        trigger="manual",
        raw=snapshot("ercot_gis_report.xlsx", URL, retrieved_at=DAY1),
    )


def test_first_run_is_ok_and_writes_every_artefact(day1, store):
    assert day1.status == "ok"
    assert set(day1.paths) == {"run", "snapshot", "normalized"}
    assert day1.paths["snapshot"].exists() and day1.paths["normalized"].exists()
    assert day1.paths["snapshot"].name.startswith("20260912T060000Z")


def test_the_source_run_record_matches_docs21_4_2(day1):
    r = day1.run
    assert r["source_id"] == SOURCE_ID
    assert r["status"] == "ok" and r["dq_status"] == "pass"
    assert r["rows_seen"] == 25 and r["rows_fetched"] == 25 and r["rows_new"] == 25
    assert r["rows_changed"] == 0 and r["rows_gone"] == 0
    assert r["egress_class"] == "plain" and r["publishable"] is True
    assert r["reuse_class"] == "open"
    assert r["worker_seconds"] >= 0 and r["model_calls"] == 0 and r["cost_usd"] == 0.0
    snap = r["snapshot"]
    assert snap["sha256"] and snap["byte_size"] == day1.paths["snapshot"].stat().st_size
    assert snap["record_count"] == 25 and snap["retrieved_at"] == "2026-09-12T06:00:00Z"
    assert snap["licence_id"] == r["licence_id"]
    assert r["parser_version"].startswith(f"{SOURCE_ID}@")


def test_the_run_record_is_json_on_disk(day1):
    record = json.loads(day1.paths["run"].read_text())
    assert record["id"] == day1.run["id"]
    assert record["dq"]["status"] == "pass"


def test_normalised_parquet_is_the_canonical_schema(day1):
    df = pd.read_parquet(day1.paths["normalized"])
    assert list(df.columns) == PROPOSAL_COLUMNS
    assert len(df) == 25
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all()


def test_a_second_identical_run_is_unchanged_and_writes_nothing_new(day1, registry, store):
    second = run(
        SOURCE_ID,
        registry=registry,
        store=store,
        raw=snapshot("ercot_gis_report.xlsx", URL, retrieved_at=DAY2),
    )
    assert second.status == "unchanged"
    assert set(second.paths) == {"run"}
    assert len(list((store.root / "normalized" / SOURCE_ID).glob("*.parquet"))) == 1


def test_a_changed_snapshot_produces_change_events(day1, registry, store):
    def mutate(ws) -> None:
        for row in ws.iter_rows(min_row=36, max_row=36):
            for cell in row:
                if isinstance(cell.value, (int, float)) and cell.value and cell.value > 10:
                    cell.value = float(cell.value) * 2  # capacity_change
                    break
        ws.delete_rows(40)  # removed

    raw2 = snapshot("ercot_gis_report.xlsx", URL, retrieved_at=DAY2)
    raw2.content = edited_workbook(mutate)
    second = run(SOURCE_ID, registry=registry, store=store, raw=raw2)

    assert second.status == "ok"
    assert second.run["snapshot"]["previous_run_id"] == day1.run["id"]
    assert "events" in second.paths
    events = pd.read_parquet(second.paths["events"])
    kinds = set(events["event_type"])
    assert "removed" in kinds
    assert second.run["rows_gone"] == 1
    assert second.run["events_emitted"] == len(events)
    assert set(events["source_id"]) == {SOURCE_ID}
    assert (events["observed_at"] == "2026-09-13T06:00:00Z").all()


def _store_under_the_pre_correction_map(day1) -> None:
    """Rewrite day 1's normalised frame as the status map before 2026-09-30 produced it:
    gridstatus's "Completed" (IA signed) read as built."""
    path = day1.paths["normalized"]
    df = pd.read_parquet(path)
    completed = df["status_raw"] == "Completed"
    df.loc[completed, "lifecycle_state"] = "built"
    df.loc[completed, "status_rule"] = "ercot.map"
    df.to_parquet(path, index=False)


def _energise_16inr0049(ws) -> None:
    """A real milestone change: 16INR0049 (IA signed, sheet row 37) is approved for energisation."""
    assert ws.cell(37, 1).value == "16INR0049" and ws.cell(31, 29).value == "Approved for Energization"
    ws.cell(37, 29).value = dt.date(2026, 9, 1)


def test_a_status_map_correction_is_reclassified_without_status_change_events(day1, registry, store):
    # docs/22 §8.1: the previous snapshot is restated under the current map before the diff, so
    # the 4 IA-only rows stored as built are reclassified silently, and only the real change on
    # 16INR0049 is published, with its before-state in the current vocabulary.
    _store_under_the_pre_correction_map(day1)
    raw2 = snapshot("ercot_gis_report.xlsx", URL, retrieved_at=DAY2)
    raw2.content = edited_workbook(_energise_16inr0049)
    second = run(SOURCE_ID, registry=registry, store=store, raw=raw2)

    assert second.status == "ok" and second.run["dq_status"] == "pass"
    assert second.run["rows_reclassified"] == 4
    assert second.run["reclassified"] == {"rows": 4, "transitions": {"built->contracted": 4}}
    checks = {c["check"]: c for c in second.run["dq"]["checks"]}
    assert checks["status_reclassified"]["level"] == "info"
    assert checks["status_reclassified"]["data"]["transitions"] == {"built->contracted": 4}
    events = pd.read_parquet(second.paths["events"])
    assert events[["event_type", "record_id", "before", "after"]].to_dict("records") == [
        {
            "event_type": "status_change",
            "record_id": f"{SOURCE_ID}:16INR0049",
            "before": "contracted",
            "after": "under_construction",
        }
    ]
    assert second.run["events_emitted"] == 1 and second.run["rows_changed"] == 1
    stored = pd.read_parquet(second.paths["normalized"])
    assert stored["lifecycle_state"].value_counts().to_dict() == {
        "built": 19,
        "contracted": 3,
        "studied": 2,
        "under_construction": 1,
    }


def test_without_restatement_the_same_correction_would_flood_status_changes(
    day1, registry, store, monkeypatch
):
    # The counterfactual the restatement exists for: diffing against the frame as stored turns the
    # mapping correction into one status_change per reclassified row.
    from pipeline.connectors.us_iso_ercot_gen_queue.connector import Connector as ErcotConnector

    monkeypatch.setattr(ErcotConnector, "restate_status", lambda self, df: None)
    _store_under_the_pre_correction_map(day1)
    raw2 = snapshot("ercot_gis_report.xlsx", URL, retrieved_at=DAY2)
    raw2.content = edited_workbook(_energise_16inr0049)
    second = run(SOURCE_ID, registry=registry, store=store, raw=raw2)

    events = pd.read_parquet(second.paths["events"])
    assert set(events["event_type"]) == {"status_change"}
    # 3 rows the source did not touch, published as built -> contracted, and the real change
    # reported from the wrong before-state (built -> under_construction).
    assert (events["before"] + "->" + events["after"]).value_counts().to_dict() == {
        "built->contracted": 3,
        "built->under_construction": 1,
    }
    assert second.run["rows_reclassified"] == 0


def test_the_run_fails_closed_on_an_unreadable_payload(registry, store):
    raw = snapshot("ercot_gis_report.xlsx", URL, retrieved_at=DAY1)
    raw.content = b"<html>maintenance</html>"
    result = run(SOURCE_ID, registry=registry, store=store, raw=raw)
    assert result.status == "failed"
    assert result.run["error_class"] == "ParseError"
    assert not (store.root / "normalized").exists()
