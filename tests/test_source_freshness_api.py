"""Source freshness on the admin source-health API and the methodology data (audit 2026-09-30 data
engineer F2): `health` stayed `ok` on sources nothing had run for 16.7 days."""

from __future__ import annotations

import datetime as dt

from services.api.conftest import make_open_licence
from services.api.coverage import source_vintages
from tests.conftest import login
from tests.test_api_admin_sources import _make_source, _operator, spec  # noqa: F401 - fixture
from tests.test_api_contract import assert_valid


def test_admin_source_health_says_how_stale_a_source_is(client, db, spec):  # noqa: F811
    lic = make_open_licence(db)
    weeks_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=20)
    src = _make_source(db, lic, cadence="weekly", implemented=True, last_success_at=weeks_ago)
    _make_source(db, lic, source_id="us.test.never", cadence="daily", implemented=True)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    body = client.get(f"/admin/v1/sources/{src.id}").json()
    assert_valid(spec, "AdminSourceDetailResponse", body)
    fresh = body["data"]["freshness"]
    assert body["data"]["health"] == "ok"  # health counts failures; freshness is the other half
    assert fresh["status"] == "stale" and fresh["alert"] is True
    assert fresh["bucket"] == "weekly" and fresh["allowance_hours"] == 192.0
    assert 479 < fresh["age_hours"] < 481

    listed = {s["source_id"]: s for s in client.get("/admin/v1/sources").json()["data"]}
    assert listed["us.test.never"]["freshness"]["status"] == "never"


def test_methodology_data_carries_freshness(db):
    lic = make_open_licence(db)
    _make_source(
        db,
        lic,
        cadence="daily",
        implemented=True,
        vintage_basis="not_stated",
        last_success_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=6),
    )
    _make_source(db, lic, source_id="us.test.context", cadence="annual", vintage_basis="not_stated")
    db.commit()
    rows = {r["source_id"]: r for r in source_vintages(db)["sources"]}
    assert rows["us.test.admin_source"]["freshness"]["status"] == "fresh"
    assert rows["us.test.context"]["freshness"]["status"] == "unscheduled"  # no connector runs it
