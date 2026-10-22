"""The API half of lane list-sort (2026-10-22): the Projects list's "Proposed online" heading sorts
through `proposed_online_date` in `PROPOSAL_SORT_ALLOWLIST`, and the RFPs & funding page's default
view is `relevance=energy` on `GET /v1/opportunities` (docs/16 U-7;
`services/api/opportunity_relevance.py`). The grid-point count sorts are tested beside the other
point sorts in `tests/test_api_interconnection_points.py`."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.ratelimit import default_limiter


def _walk(client: TestClient, url: str, **params: Any) -> list[dict[str, Any]]:
    """Every row of a list, one page of one row at a time, following the cursor."""
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        default_limiter.reset()
        query: dict[str, Any] = {"limit": 1, **params}
        if cursor:
            query["cursor"] = cursor
        resp = client.get(url, params=query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows.extend(body["data"])
        cursor = body["page"]["next_cursor"]
        if not cursor:
            return rows


# ------------------------------------------------------------------ proposals: proposed online date
def test_proposals_sort_by_proposed_online_date_both_ways_with_undated_rows_last(
    client: TestClient, db: Session
) -> None:
    src = make_public_source(db, make_open_licence(db))
    dates = {"1": dt.date(2027, 6, 1), "2": dt.date(2029, 1, 15), "3": None, "4": dt.date(2026, 12, 31)}
    for suffix, date in dates.items():
        make_visible_proposal(db, src, public_id_suffix=suffix, proposed_online_date=date)
    db.commit()

    latest_first = _walk(client, "/v1/proposals", sort="-proposed_online_date")
    assert [r["proposed_online_date"] for r in latest_first] == [
        "2029-01-15",
        "2027-06-01",
        "2026-12-31",
        None,
    ]
    earliest_first = _walk(client, "/v1/proposals", sort="proposed_online_date")
    assert [r["proposed_online_date"] for r in earliest_first] == [
        "2026-12-31",
        "2027-06-01",
        "2029-01-15",
        None,
    ]
    # The allowlist grew by one field, not into "any column".
    resp = client.get("/v1/proposals", params={"sort": "-commercial_operation_date"})
    assert resp.status_code == 400 and resp.json()["code"] == "validation_error"


# ------------------------------------------------------------------ opportunities: relevance
def _notices(db: Session) -> dict[str, str]:
    """Three open notices: an energy one, a fuel purchase and an unrelated service."""
    src = make_public_source(db, make_open_licence(db))
    titles = {
        "solar": ("Solar PV plant, 50 MW", ["solar_pv"]),
        "fuel": ("Supply of diesel fuel for municipal vehicles", []),
        "cleaning": ("Office cleaning services", []),
    }
    out: dict[str, str] = {}
    for n, (key, (title, techs)) in enumerate(titles.items(), start=1):
        opp = make_visible_opportunity(db, src, public_id_suffix=str(n))
        opp.title = title
        opp.technologies = techs
        out[key] = opp.public_id
    db.commit()
    return out


def test_relevance_energy_keeps_only_energy_notices_in_rows_and_count(
    client: TestClient, db: Session
) -> None:
    ids = _notices(db)
    energy = client.get("/v1/opportunities", params={"relevance": "energy", "include": "count"}).json()
    assert [o["public_id"] for o in energy["data"]] == [ids["solar"]]
    assert energy["meta"]["total"] == 1
    for every in ({"relevance": "all", "include": "count"}, {"include": "count"}):
        body = client.get("/v1/opportunities", params=every).json()
        assert {o["public_id"] for o in body["data"]} == set(ids.values()) and body["meta"]["total"] == 3


def test_relevance_energy_pages_with_the_cursor(client: TestClient, db: Session) -> None:
    ids = _notices(db)
    src = make_public_source(db, make_open_licence(db, id_="open-lic-2"), id_="us.test.second")
    wind = make_visible_opportunity(db, src, public_id_suffix="9")
    wind.title = "Onshore wind farm grid connection"
    db.commit()
    rows = _walk(client, "/v1/opportunities", relevance="energy")
    assert sorted(r["public_id"] for r in rows) == sorted([ids["solar"], wind.public_id])


def test_relevance_refuses_an_unknown_value_and_an_energy_csv(client: TestClient, db: Session) -> None:
    _notices(db)
    resp = client.get("/v1/opportunities", params={"relevance": "renewables"})
    assert resp.status_code == 400 and resp.json()["code"] == "validation_error"
    # The CSV twin cannot apply the verdict (the export builder does not evaluate it), so it says so
    # instead of returning every notice under an `energy` request.
    resp = client.get("/v1/opportunities", params={"relevance": "energy"}, headers={"Accept": "text/csv"})
    assert resp.status_code == 400 and resp.json()["code"] == "validation_error"
    assert "relevance" in resp.text
