"""PostGIS round trips for `services.db.types.GeographyPoint` / `GeographyLine` (2026-09-19).

Runs only when `POSTGIS_TEST_URL` names a reachable Postgres + PostGIS database that is at
migration head (the CI one: `postgresql+psycopg://infraque:infraque-ci-only@127.0.0.1:55432/
infraque_ci`); skipped otherwise, so the SQLite suite is unchanged. Each test inserts an `asset`
row through the ORM, reads it back through the ORM and through raw SQL, and cleans up after itself.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.db.models import Asset, Licence, Source, new_uuid
from services.db.session import get_engine, get_sessionmaker
from services.ids import public_id

POSTGIS_URL = os.environ.get("POSTGIS_TEST_URL")
pytestmark = pytest.mark.skipif(not POSTGIS_URL, reason="POSTGIS_TEST_URL not set")

LICENCE_ID = "lic-types-postgis-test"
SOURCE_ID = "test.types.postgis"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine(POSTGIS_URL)
    s = get_sessionmaker(engine)()
    s.merge(Licence(id=LICENCE_ID, name="test", reuse_class="open"))
    s.flush()
    if s.get(Source, SOURCE_ID) is None:
        cols = {c.name for c in Source.__table__.columns}
        wanted = {
            "id": SOURCE_ID,
            "name": "test",
            "category": "registry",
            "url": "https://example.org",
            "access": "api",
            "cadence": "annual",
            "tier": 2,
            "egress": "plain",
            "licence_id": LICENCE_ID,
        }
        s.add(Source(**{k: v for k, v in wanted.items() if k in cols}))
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.execute(text("DELETE FROM asset WHERE source_id = :s"), {"s": SOURCE_ID})
        s.execute(text("DELETE FROM source WHERE id = :s"), {"s": SOURCE_ID})
        s.execute(text("DELETE FROM licence WHERE id = :l"), {"l": LICENCE_ID})
        s.commit()
        s.close()


def _asset(**overrides: object) -> Asset:
    aid = new_uuid()
    base: dict[str, object] = {
        "id": aid,
        "public_id": public_id("asset", aid),
        "slug": f"types-test-{str(aid)[:8]}",
        "asset_type": "gas_pipeline",
        "source_asset_id": f"types-test-{str(aid)[:8]}",
        "name": "types test",
        "status": "operating",
        "country": "US",
        "source_id": SOURCE_ID,
        "source_url": "https://example.org",
        "retrieved_at": dt.datetime.now(dt.UTC),
        "licence_id": LICENCE_ID,
        "attributes": {},
    }
    base.update(overrides)
    return Asset(**base)


def test_point_tuple_round_trips_on_postgis(session: Session) -> None:
    a = _asset(geom=(-104.280599, 40.988289))
    session.add(a)
    session.commit()
    session.expire_all()
    row = session.get(Asset, a.id)
    assert row is not None
    assert row.geom == (-104.280599, 40.988289)
    assert session.execute(select(Asset.geom).where(Asset.id == a.id)).scalar() == (-104.280599, 40.988289)
    wkt, srid = session.execute(
        text("SELECT ST_AsText(geom::geometry), ST_SRID(geom::geometry) FROM asset WHERE id = :i"),
        {"i": a.id},
    ).one()
    assert wkt == "POINT(-104.280599 40.988289)" and srid == 4326


def test_point_accepts_list_and_wkt_string(session: Session) -> None:
    a = _asset(geom=[-96.9, 40.3])
    session.add(a)
    session.commit()
    a.geom = "POINT(-95.5 41.25)"
    session.commit()
    session.expire_all()
    row = session.get(Asset, a.id)
    assert row is not None and row.geom == (-95.5, 41.25)


def test_multilinestring_round_trips_on_postgis(session: Session) -> None:
    wkt = "MULTILINESTRING((-104.28 40.99, -104.1 41), (-101.25 40.84, -101.2 40.9))"
    a = _asset(geom=None, geom_line=wkt)
    session.add(a)
    session.commit()
    session.expire_all()
    row = session.get(Asset, a.id)
    assert row is not None and row.geom_line == wkt
    assert session.execute(select(Asset.geom_line).where(Asset.id == a.id)).scalar() == wkt
    parts = session.execute(
        text("SELECT ST_NumGeometries(geom_line::geometry) FROM asset WHERE id = :i"), {"i": a.id}
    ).scalar()
    assert parts == 2


def test_linestring_is_promoted_to_multilinestring(session: Session) -> None:
    a = _asset(geom=None, geom_line="LINESTRING(-100 40, -99.5 40.5)")
    session.add(a)
    session.commit()
    session.expire_all()
    row = session.get(Asset, a.id)
    assert row is not None and row.geom_line == "MULTILINESTRING((-100 40, -99.5 40.5))"
