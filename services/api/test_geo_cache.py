"""The proposal map's data-version cache (`services/api/geo_cache.py`; backend audit 2026-10-07,
PERF-2, PERF-2a): a pan reuses the whole-set work, any write the map could see invalidates it, and a
registry-only hidden source no longer switches the per-row field gate on."""

from __future__ import annotations

import gc
import re
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import geo_cache
from services.api.conftest import (
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db

WORLD = "-180,-85,180,85"
TEXAS = "-107,25,-93,37"
_VOLATILE = re.compile(rb'"(request_id|generated_at|data_as_of)":"[^"]*"')


def _body(resp: Any) -> bytes:
    assert resp.status_code == 200, resp.text
    return bytes(_VOLATILE.sub(rb'"\1":"-"', resp.content))


def _seed(db: Session, n: int = 6) -> Source:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i in range(n):
        loc = make_location(db, src, lic, geom=(-97.0 + i, 30.0 + i / 10), precision="exact")
        make_visible_proposal(db, src, public_id_suffix=str(i + 1), location=loc)
    db.commit()
    return src


@pytest.fixture()
def selects(db_sessionmaker: sessionmaker[Session]) -> Any:
    seen: list[str] = []

    def count(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    engine = db_sessionmaker.kw["bind"]
    sa.event.listen(engine, "before_cursor_execute", count)
    yield seen
    sa.event.remove(engine, "before_cursor_execute", count)


def test_a_pan_reuses_the_whole_set_work_and_answers_byte_for_byte(
    client: TestClient, db: Session, selects: list[str]
) -> None:
    _seed(db)
    geo_cache.reset()
    selects.clear()
    cold = _body(client.get(f"/v1/proposals/geo?bbox={TEXAS}&zoom=4"))
    cold_selects = len(selects)
    selects.clear()
    warm_world = _body(client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=4"))
    warm_selects = len(selects)
    selects.clear()
    warm = _body(client.get(f"/v1/proposals/geo?bbox={TEXAS}&zoom=4"))
    assert warm == cold
    # The whole-set query (visibility, filters, totals) and the licence aggregate are not re-run.
    assert warm_selects < cold_selects, (cold_selects, warm_selects)
    assert not any("GROUP BY" in s for s in selects)
    geo_cache.reset()
    assert _body(client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=4")) == warm_world


def _ids(client: TestClient, query: str = "") -> set[str]:
    body = client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=8{query}").json()
    return {f["id"] for f in body["data"]["features"]}


def test_every_write_the_map_could_see_invalidates_it(client: TestClient, db: Session) -> None:
    src = _seed(db, n=3)
    first = _ids(client)
    assert len(first) == 3

    # A link deactivated (no proposal or source timestamp moves): the record loses its only link.
    link = db.scalars(sa.select(ProposalSource).order_by(ProposalSource.source_record_id)).first()
    assert link is not None
    link.active = False
    db.commit()
    assert len(_ids(client)) == 2

    # A raw statement on another table the map reads (a placement moved off-map).
    db.execute(sa.text("UPDATE location SET geom = NULL"))
    db.commit()
    assert _ids(client) == set()

    # A new record, and a source unpublished.
    loc = make_location(db, src, src.licence, geom=(-90.0, 35.0), precision="exact")
    make_visible_proposal(db, src, public_id_suffix="9", location=loc)
    db.commit()
    assert len(_ids(client)) == 1
    db.get(Source, src.id).publish_state = "ingest_only"  # type: ignore[union-attr]
    db.commit()
    assert _ids(client) == set()


def test_answers_are_kept_apart_by_filter_and_tier(client: TestClient, db: Session) -> None:
    _seed(db, n=4)
    everything = _ids(client)
    one = db.scalars(sa.select(Proposal).order_by(Proposal.public_id)).first()
    assert one is not None
    only = _ids(client, f"&slug={one.slug}")
    assert only == {one.public_id} and len(everything) == 4
    assert _ids(client) == everything


def test_entries_expire_and_the_least_recent_goes_first() -> None:
    now = [0.0]
    cache = geo_cache.TtlLru(max_entries=2, ttl=60)
    cache.clock = lambda: now[0]
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)  # evicts "b", the least recently used
    assert cache.get("b") is None and cache.get("a") == 1
    now[0] = 61.0
    assert cache.get("a") is None and cache.get("c") is None


def test_a_new_engine_never_inherits_an_old_engines_answers() -> None:
    tokens = set()
    for _ in range(3):
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        with get_sessionmaker(engine)() as s:
            tokens.add(geo_cache.engine_token(s))
        engine.dispose()
        del engine
        gc.collect()
    assert len(tokens) == 3


@pytest.mark.parametrize(
    ("statement", "watched"),
    [
        ("UPDATE proposal SET name_canonical=? WHERE proposal.id = ?", True),
        ('TRUNCATE "event", "proposal_source" RESTART IDENTITY CASCADE', True),
        ("INSERT INTO location (id, geom) VALUES (?, ?)", True),
        ("DELETE FROM organization_alias WHERE id = ?", True),
        ("UPDATE api_key SET last_used_at=? WHERE api_key.id = ?", False),
        ("INSERT INTO event (id, seq, source_id, licence_id) VALUES (?, ?, ?, ?)", False),
        ("SELECT proposal.id FROM proposal", False),
    ],
)
def test_which_statements_bump_the_write_generation(statement: str, watched: bool) -> None:
    assert geo_cache._names_watched_table(statement) is watched


# --------------------------------------------------------------------------- PERF-2a
def test_a_registry_only_hidden_source_does_not_switch_the_field_gate_on(
    client: TestClient, db: Session, selects: list[str]
) -> None:
    _seed(db, n=3)
    shown = _body(client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=4"))
    lic = make_open_licence(db, "registry-lic")
    make_public_source(db, lic, id_="gb.test.registry_only").publish_state = "ingest_only"
    db.commit()
    geo_cache.reset()
    selects.clear()
    assert _body(client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=4")) == shown
    main = [s for s in selects if "FROM proposal LEFT OUTER JOIN location" in s]
    assert main, selects
    assert not any("json_extract" in s or "field_provenance" in s for s in main)


def test_a_hidden_source_that_placed_or_supplied_a_value_stays_hidden(db: Session) -> None:
    src = _seed(db, n=1)
    lic = make_open_licence(db, "other-lic")
    placer = make_public_source(db, lic, id_="us.test.placer")
    supplier = make_public_source(db, lic, id_="us.test.supplier")
    unused = make_public_source(db, lic, id_="us.test.unused")
    prop = db.scalars(sa.select(Proposal)).one()
    prop.location = make_location(db, placer, lic, geom=(-97.0, 30.0), precision="exact")
    prop.field_provenance = {"capacity_mw": {"source_id": src.id, "source_ids": [src.id, supplier.id]}}
    db.commit()
    hidden = frozenset({placer.id, supplier.id, unused.id})
    assert geo_cache.unused_hidden_sources(db, hidden) == {unused.id}
