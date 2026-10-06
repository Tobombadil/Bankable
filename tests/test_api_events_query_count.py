"""`/v1/events` and the events feed look their subjects up once per page, not once per event
(backend audit 2026-09-30 F11: `_subject_info` per row, 401 queries for a 200-event page on the
audit store, p50 295 ms; batched, 3 queries and 73 ms)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session, sessionmaker

from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Source


@pytest.fixture()
def statements(db_sessionmaker: sessionmaker[Session]) -> Iterator[list[str]]:
    engine = db_sessionmaker.kw["bind"]
    seen: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", count)
    yield seen
    sa_event.remove(engine, "before_cursor_execute", count)


def _add_events(db: Session, source: Source, suffixes: range) -> None:
    for i in suffixes:
        make_event(db, make_visible_proposal(db, source, public_id_suffix=str(i)), source)
    db.commit()


@pytest.mark.parametrize("url", ["/v1/events?limit=200", "/feeds/events.json"])
def test_the_query_count_does_not_grow_with_the_page(
    url: str, client: TestClient, db: Session, statements: list[str]
) -> None:
    source = make_public_source(db, make_open_licence(db))
    _add_events(db, source, range(1, 6))
    statements.clear()
    small = client.get(url)
    assert small.status_code == 200
    few = len(statements)

    _add_events(db, source, range(100, 130))
    statements.clear()
    large = client.get(url)
    assert large.status_code == 200
    assert len(statements) == few, (few, len(statements))
