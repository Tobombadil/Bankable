"""A price store built from the recorded fixtures by the real runner (behind `FakeHttp`), and a
database loaded from it by the real loader and statistics job: what the price tests of
`services/prices`, `services/api` and `web` read. Seeding runs the whole chain, so a change at
any stage shows in the figures the API serves."""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import io
import pathlib
import sqlite3
import threading
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pipeline.connectors.prices.conftest import FakeHttp, fixture_bytes
from pipeline.connectors.prices.runner import run
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store

NOW = dt.datetime(2026, 10, 22, 22, 0, tzinfo=dt.UTC)
AS_OF = NOW.date()
NC = frozenset({"open", "attribution", "noncommercial"})
COMMERCIAL = frozenset({"open", "attribution"})


def _oasis(**kw: Any) -> tuple[int, bytes, dict[str, str]]:
    query = (kw.get("params") or {}).get("queryname")
    name = {"PRC_LMP": "caiso_dam_2d.zip", "PRC_RTPD_LMP": "caiso_rtpd_1d.zip"}[str(query)]
    return 200, fixture_bytes(name), {}


def _nyiso_month(dataset: str) -> Any:
    """October 2026's zip as NYISO builds it, holding the recorded day's own csv file."""

    def answer(**_kw: Any) -> tuple[int, bytes, dict[str, str]]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"20261021{dataset}_zone.csv", fixture_bytes(f"nyiso_20261021{dataset}_zone.csv"))
        return 200, buf.getvalue(), {}

    return answer


ROUTES: dict[str, dict[str, Any]] = {
    "us.iso.ercot.prices": {
        "20261021_real_time_spp.html": "ercot_20261021_real_time_spp.html",
        "20261022_dam_spp.html": "ercot_20261022_dam_spp.html",
        "cdr/html/real_time_spp.html": "ercot_real_time_spp.html",
    },
    "us.iso.caiso.oasis_prices": {"oasisapi/SingleZip": _oasis},
    "us.iso.nyiso.prices": {
        "20261001damlbmp_zone_csv.zip": _nyiso_month("damlbmp"),
        "20261001rtlbmp_zone_csv.zip": _nyiso_month("rtlbmp"),
    },
    "gb.elexon.bmrs_prices": {"system-prices/2026-10-21": "elexon_system_prices_20261021.json"},
    "us.iso.spp.prices": {
        "DA_Features/MapServer": "spp_DA_Features_hubs.json",
        "RTBM_Features/MapServer": "spp_RTBM_Features_hubs.json",
    },
    "us.eia.retail_sales": {
        "sales_revenue.xlsx": lambda **_k: (
            200,
            fixture_bytes("eia_sales_revenue_trimmed.xlsx"),
            {"Last-Modified": "Thu, 24 Sep 2026 13:48:22 GMT"},
        )
    },
}
MODES = {
    "us.iso.ercot.prices": ("daily", "latest"),
    "us.iso.caiso.oasis_prices": ("daily",),
    "us.iso.nyiso.prices": ("daily",),
    "gb.elexon.bmrs_prices": ("daily",),
    "us.iso.spp.prices": ("daily", "latest"),
    "us.eia.retail_sales": ("daily",),
}


@functools.lru_cache(maxsize=1)
def _registry() -> Registry:
    return Registry()


def build_store(root: pathlib.Path) -> Store:
    store = Store(root)
    for source_id, modes in MODES.items():
        for mode in modes:
            result = run(
                source_id,
                mode,
                store=store,
                http=FakeHttp(ROUTES[source_id]),
                now=NOW,
                publishable=NC,
                registry=_registry(),
            )
            if result.status not in ("ok", "unchanged"):
                raise RuntimeError(
                    f"seeding {source_id} {mode}: {result.status} {result.record.get('error')}"
                )
    return store


def load_all(session: Session, store: Store, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every source into `session` (SPP under the noncommercial posture, as the live site runs),
    then the statistics as of `AS_OF`."""
    import services.ingest.loader as loader
    from services.posture import gated_reuse_classes
    from services.prices.load import load_source
    from services.prices.stats import run_stats

    monkeypatch.setattr(loader, "PUBLISHABLE_REUSE", NC)
    monkeypatch.setattr(loader, "GATED_REUSE", frozenset(gated_reuse_classes("noncommercial")))
    registry = _registry()
    reports = {
        # `now` is the recording time, so "the latest interval that has begun" does not move with
        # the clock the suite runs on (SPP's posted day-ahead hours are all after it).
        source_id: load_source(session, source_id, store=store, registry=registry, now=NOW)
        for source_id in MODES
    }
    reports["stats"] = run_stats(session, as_of=AS_OF)
    session.commit()
    return reports


def seeded_template(store: Store) -> tuple[sqlite3.Connection, dict[str, Any]]:
    """`load_all` once into an in-memory database, kept as a raw connection to copy from: each
    test then gets its own copy (`engine_from`) at the cost of a backup, not of a load."""
    from services.db.session import get_sessionmaker
    from tests.db_template import disposing, fresh_engine

    patch = pytest.MonkeyPatch()
    try:
        with disposing(fresh_engine()) as engine:
            with get_sessionmaker(engine)() as session:
                reports = load_all(session, store, patch)
            pooled = engine.raw_connection()
            try:
                source = pooled.driver_connection
                if not isinstance(source, sqlite3.Connection):
                    raise TypeError("the price template needs a SQLite connection")
                copy = sqlite3.connect(":memory:", check_same_thread=False)
                source.backup(copy)
            finally:
                pooled.close()
    finally:
        patch.undo()
    return copy, reports


def engine_from(template: sqlite3.Connection) -> Engine:
    """A new in-memory engine holding a copy of `template` (as `tests/db_template.fresh_engine`)."""

    def creator() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        with _COPY_LOCK:
            template.backup(conn)
        return conn

    engine = sa.create_engine("sqlite+pysqlite://", creator=creator, poolclass=StaticPool, future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


_COPY_LOCK = threading.Lock()


@contextlib.contextmanager
def seeded_sessionmaker(template: sqlite3.Connection) -> Iterator[sessionmaker[Session]]:
    """A sessionmaker on a fresh copy of `template`, disposed of on exit; for the API and web
    tests, which reach the database layer only through this module."""
    from services.db.session import get_sessionmaker
    from tests.db_template import disposing

    with disposing(engine_from(template)) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture(scope="session")
def price_store(tmp_path_factory: pytest.TempPathFactory) -> Store:
    return build_store(tmp_path_factory.mktemp("price-store"))


@pytest.fixture(scope="session")
def price_template(price_store: Store) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]]]:
    template, reports = seeded_template(price_store)
    yield template, reports
    template.close()


@pytest.fixture()
def price_session(
    price_template: tuple[sqlite3.Connection, dict[str, Any]],
) -> Iterator[tuple[Session, dict[str, Any]]]:
    from services.db.session import get_sessionmaker
    from tests.db_template import disposing

    template, reports = price_template
    with disposing(engine_from(template)) as engine:
        with get_sessionmaker(engine)() as session:
            yield session, reports
