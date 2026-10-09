"""Per-source last success, last loaded run and freshness on the public surfaces (expert review
2026-10-07: `/v1/sources` answered `last_success_at: null` for every source while each had a
recorded successful run, and `/v1/health` reported the request time as the data's date)."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Source, SourceRun

UTC = dt.UTC


def _seed(db: Session) -> Source:
    src = make_public_source(db, make_open_licence(db))
    src.implemented = True
    src.cadence = "weekly"
    src.last_success_at = None  # fetched through the CLI: the runner's column was never written
    src.last_loaded_ts = "20260913T202521Z"
    src.vintage_basis = "not_stated"
    make_visible_proposal(db, src)
    old = dt.datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    ok = dt.datetime(2026, 9, 13, 20, 25, 23, tzinfo=UTC)
    db.add(SourceRun(source_id=src.id, started_at=old, finished_at=old, status="failed"))
    db.add(SourceRun(source_id=src.id, started_at=ok - dt.timedelta(seconds=9), finished_at=ok, status="ok"))
    db.commit()
    return src


def test_sources_report_the_last_successful_run_the_loaded_run_and_staleness(
    client: TestClient, db: Session
) -> None:
    src = _seed(db)
    listed = next(s for s in client.get("/v1/sources").json()["data"] if s["source_id"] == src.id)
    one = client.get(f"/v1/sources/{src.id}").json()["data"]
    for row in (listed, one):
        assert row["last_success_at"] == "2026-09-13T20:25:23Z"
        assert row["last_loaded_at"] == "2026-09-13T20:25:21Z"
        fr = row["freshness"]
        assert fr["bucket"] == "weekly" and fr["allowance_hours"] == 192.0
        # Weeks past a weekly allowance by the time any reader runs this.
        assert fr["status"] == "stale" and fr["alert"] is True
        assert fr["last_success_at"] == "2026-09-13T20:25:23Z"


def test_coverage_carries_the_same_facts(client: TestClient, db: Session) -> None:
    src = _seed(db)
    rows = client.get("/v1/coverage").json()["data"]["vintage"]["sources"]
    row = next(r for r in rows if r["source_id"] == src.id)
    assert row["last_success_at"] == "2026-09-13T20:25:23Z"
    assert row["last_loaded_at"] == "2026-09-13T20:25:21Z"
    assert row["fetched_at"].startswith("2026-09-13T20:25:23")
    assert row["freshness"]["status"] == "stale"


def test_health_reports_the_datas_age_not_the_request_time(client: TestClient, db: Session) -> None:
    assert client.get("/v1/health").json()["data_as_of"] is None  # nothing loaded
    src = _seed(db)
    newer = make_public_source(db, make_open_licence(db, "l2"), id_="us.test.newer")
    newer.last_success_at = dt.datetime(2026, 10, 1, tzinfo=UTC)
    newer.last_loaded_ts = "20261001T000000Z"
    db.commit()
    body = client.get("/v1/health").json()
    assert body["data_as_of"] == "2026-09-13T20:25:23Z"
    assert body["data_as_of_newest"] == "2026-10-01T00:00:00Z"
    assert body["live_as_of"] != body["data_as_of"]
    assert src.id  # the older source bounds the data


def test_a_note_about_a_source_with_no_published_rows_retires_when_they_publish(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `source_without_published_rows` condition for `data/vocabulary/coverage_notes.yaml`
    (frontend lane L5's ERCOT large-load note): the note holds while the named source supplies no
    row a public reader is served, and drops out the moment one is."""
    from services.api import coverage as coverage_module

    note = {
        "id": "n",
        "headline": "h",
        "body": "b",
        "written": "2026-10-07",
        "applies_to": {"source_without_published_rows": "us.test.public_source"},
    }
    monkeypatch.setattr(coverage_module, "_notes", lambda: [note])
    src = make_public_source(db, make_open_licence(db))
    db.commit()
    assert [n["id"] for n in coverage_module.coverage(db)["notes"]] == ["n"]
    prop = make_visible_proposal(db, src)
    prop.publish_state = "pending_review"  # stored but not published: the note still holds
    db.commit()
    assert [n["id"] for n in coverage_module.coverage(db)["notes"]] == ["n"]
    prop.publish_state = "public"
    db.commit()
    facts = coverage_module.coverage(db)
    assert facts["notes"] == [] and "us.test.public_source" in facts["sources"]["published_source_ids"]
