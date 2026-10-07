"""Keyset pagination over NOT NULL sort columns (backend audit 2026-10-07, PERF-3): no `NULLS LAST`
and no dead `IS NULL` arm, so the list sort indexes of migration 0035 can serve a page; every row
still comes back exactly once, in the same order, whatever the ties."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Proposal

UTC = dt.UTC


def _page_through(client: TestClient, sort: str, limit: int) -> list[str]:
    seen: list[str] = []
    cursor = None
    for _ in range(50):
        params: dict[str, Any] = {"sort": sort, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/proposals", params=params).json()
        seen += [p["public_id"] for p in body["data"]]
        cursor = body["page"]["next_cursor"]
        if not cursor:
            return seen
    raise AssertionError("did not finish")


@pytest.mark.parametrize(
    "sort", ["-last_changed", "last_changed", "name_canonical", "-first_seen", "-capacity_mw"]
)
def test_paging_with_ties_returns_every_row_once_in_order(
    sort: str, client: TestClient, db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    src = make_public_source(db, make_open_licence(db))
    stamp = dt.datetime(2026, 9, 1, tzinfo=UTC)
    for i in range(11):
        prop = make_visible_proposal(db, src, public_id_suffix=str(i + 1))
        prop.last_changed = stamp + dt.timedelta(days=i // 4)  # ties of four
        prop.first_seen = stamp - dt.timedelta(days=i // 3)
        prop.name_canonical = f"Project {'ABC'[i % 3]}"
        prop.capacity_mw = None if i % 5 == 0 else float(i // 2)
    db.commit()
    statements: list[str] = []
    engine = db_sessionmaker.kw["bind"]

    def grab(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", grab)
    try:
        paged = _page_through(client, sort, 3)
    finally:
        sa.event.remove(engine, "before_cursor_execute", grab)
    whole = [
        p["public_id"]
        for p in client.get("/v1/proposals", params={"sort": sort, "limit": 200}).json()["data"]
    ]
    assert paged == whole and len(set(paged)) == 11

    column = sort.lstrip("-")
    pages = [s for s in statements if "ORDER BY proposal." + column in s]
    assert pages
    if column == "capacity_mw":  # nullable: NULLs last both ways, resumed past them
        assert all("NULLS LAST" in s for s in pages)
    else:
        assert not any("NULLS LAST" in s for s in pages)
        assert not any(f"proposal.{column} IS NULL" in s for s in pages)
        resumed = [s for s in pages if f"(proposal.{column}, proposal.id)" in s]
        assert len(resumed) == len(pages) - 1


def test_not_null_detection_reads_the_column_declaration() -> None:
    from services.api.pagination import _is_not_null

    assert _is_not_null(Proposal.last_changed) and _is_not_null(Proposal.name_canonical)
    assert not _is_not_null(Proposal.capacity_mw)
