"""Nearby-proposal lists page with a real cursor, count their scope before the cap, and hydrate only
the page they return (backend audit 2026-10-07, PERF-4, API-7: `has_more: true` came with
`next_cursor: null`, `assets_in_scope` could never exceed the cap, and every candidate in an owner's
box was loaded as a full record)."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import assets as assets_module
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)


def _seed(db: Session) -> tuple[str, str]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db)
    west = make_asset(db, src, lic, source_asset_id="1", geom=(-100.0, 32.0))
    east = make_asset(db, src, lic, source_asset_id="2", geom=(-90.0, 35.0), name="East Plant")
    make_asset_owner(db, west, org, src, lic)
    make_asset_owner(db, east, org, src, lic)
    n = 0
    for lon, lat in ((-100.0, 32.0), (-90.0, 35.0)):
        for i in range(7):
            n += 1
            # Two proposals share every point, so equal distances are common.
            loc = make_location(db, src, lic, geom=(lon + (i // 2) * 0.01, lat), precision="exact")
            make_visible_proposal(db, src, public_id_suffix=str(n), location=loc)
    db.commit()
    return org.public_id, west.public_id


def _walk(client: TestClient, path: str, limit: int, **params: Any) -> tuple[list[str], list[float]]:
    ids: list[str] = []
    distances: list[float] = []
    cursor = None
    for _ in range(20):
        query = {"limit": limit, "radius_km": 50, **params}
        if cursor:
            query["cursor"] = cursor
        resp = client.get(path, params=query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids += [row["public_id"] for row in body["data"]]
        distances += [row["distance_km"] for row in body["data"]]
        cursor = body["page"]["next_cursor"]
        assert bool(cursor) == body["page"]["has_more"]
        if not cursor:
            return ids, distances
    raise AssertionError("did not finish")


@pytest.mark.parametrize("which", ["organization", "asset"])
def test_a_cursor_walks_every_nearby_proposal_once_nearest_first(
    which: str, client: TestClient, db: Session
) -> None:
    org_id, asset_id = _seed(db)
    path = (
        f"/v1/organizations/{org_id}/nearby-proposals"
        if which == "organization"
        else f"/v1/assets/{asset_id}/nearby-proposals"
    )
    whole, _ = _walk(client, path, 200)
    paged, distances = _walk(client, path, 3)
    assert paged == whole
    assert len(set(paged)) == len(paged) == (14 if which == "organization" else 7)
    assert distances == sorted(distances)


def test_a_malformed_nearby_cursor_is_a_400(client: TestClient, db: Session) -> None:
    org_id, _ = _seed(db)
    from services.api.pagination import encode_cursor

    for bad in ("not-a-cursor", encode_cursor("far", "prop_x")):
        resp = client.get(f"/v1/organizations/{org_id}/nearby-proposals", params={"cursor": bad})
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_cursor"


def test_assets_in_scope_counts_what_the_cap_was_taken_from(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, _ = _seed(db)
    monkeypatch.setattr(assets_module, "ORG_NEARBY_ASSET_CAP", 1)
    totals = client.get(f"/v1/organizations/{org_id}/nearby-proposals").json()["totals"]
    assert totals["assets_considered"] == 1
    assert totals["assets_in_scope"] == 2


def test_only_the_returned_page_is_loaded_as_records(
    client: TestClient, db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    org_id, _ = _seed(db)
    statements: list[str] = []
    engine = db_sessionmaker.kw["bind"]

    def grab(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", grab)
    try:
        body = client.get(f"/v1/organizations/{org_id}/nearby-proposals", params={"limit": 2}).json()
    finally:
        sa.event.remove(engine, "before_cursor_execute", grab)
    assert len(body["data"]) == 2
    link_loads = [
        s for s in statements if "FROM proposal_source" in s and "proposal_source.proposal_id IN" in s
    ]
    assert len(link_loads) == 1
    # The links are loaded for the two returned rows only, not for all 14 candidates.
    assert link_loads[0].count("?") <= 4, link_loads[0]
