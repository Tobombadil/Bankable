"""The application on Postgres + PostGIS, compared with SQLite (audit A1, A2, A5; 2026-09-30).

Every other API test runs on SQLite. Postgres-only branches (`_is_postgres` in
`services/api/assets.py`) and Postgres-only behaviour (a real identity sequence, a real
`timestamptz`) were therefore never run before production. The audit found three public routes
answering 500 on Postgres and 200 on SQLite, and concurrent event writers failing on Postgres
only. This module runs those surfaces on Postgres and compares their bodies with SQLite's.

It runs only when `POSTGIS_TEST_URL` names a Postgres 16 + PostGIS database already migrated to
head (CI: the `postgres` job in `.github/workflows/ci.yml` migrates, then runs this module). It
is skipped otherwise, so the SQLite suite is unchanged. It **empties every application table**
before each test, so it refuses a database whose name does not contain `test` or `ci` as a
`_`-separated word.

What it pins:
- `/v1/assets/geo` and `/v1/context/plants/geo`, individual and clustered, answer 200 and equal
  SQLite's body (`ST_X` over a geography raised `UndefinedFunction` before).
- `/v1/organizations/{id}/nearby-proposals` answers 200 and equals SQLite's body, including a
  proposal the old geography envelope dropped (`DISTINCT` + `ORDER BY asset_owner.id` raised
  `InvalidColumnReference` before; the geography envelope is `_location_in_bbox_clause`'s note).
- Concurrent event writers all commit with distinct `seq` values from the database sequence
  (`UniqueViolation event_seq_key` before), and migration 0027's sequence repair.
- A handful of list endpoints return the same body on both backends, and every timestamp in them
  carries an offset on both.
- Watermark readers (webhook enqueue, saved-search alerts, social drafts, `/v1/events?since=`) do
  not pass a seq an open transaction commits after a higher one (backend audit F5, 2026-10-06), and
  migration 0032's horizon trigger is installed with the reader's constants.

Known differences, documented rather than asserted equal:
- Coordinates: a point bound to PostGIS is written as 7-decimal EWKT (`services/db/types.py::
  _fmt_coord`, about 1 cm), while SQLite keeps the float as given. Measured on the audit store:
  largest difference 1.3e-10 degrees in any served coordinate or centroid. The seed below uses
  coordinates with at most 7 decimals; floats are compared to 1e-9.
- `/v1/proposals/geo` and its `licence_summary.sources` have no `ORDER BY` (`services/api/
  records.py`), so they are compared as sets here, not in order.
"""

from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from services.api import assets as assets_module
from services.api.app import app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.base import Base
from services.db.models import (
    Account,
    Asset,
    AssetOwner,
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    SavedSearch,
    Source,
    WebhookDelivery,
    WebhookEndpoint,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id

POSTGIS_URL = os.environ.get("POSTGIS_TEST_URL")
pytestmark = pytest.mark.skipif(not POSTGIS_URL, reason="POSTGIS_TEST_URL not set")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
UTC = dt.UTC
#: Every time-valued column the seed does not set is overwritten with this, so both backends hold
#: identical rows and bodies can be compared whole.
FIXED = dt.datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WORLD_BBOX = "-180,-85,180,85"
VOLATILE_KEYS = frozenset({"generated_at", "data_as_of", "live_as_of", "request_id"})
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
OFFSET = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


# ----------------------------------------------------------------------------------- engines
def _require_disposable(url: str) -> None:
    name = make_url(url).database or ""
    if not {"test", "ci"} & set(name.lower().split("_")):
        raise RuntimeError(
            f"tests/test_postgres.py empties every table; refusing database {name!r} "
            "(its name must contain `test` or `ci` as a `_`-separated word)"
        )


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[sa.Engine]:
    assert POSTGIS_URL is not None
    _require_disposable(POSTGIS_URL)
    engine = get_engine(POSTGIS_URL)
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert current == head, f"migrate the test database first: at {current}, head is {head}"
    yield engine
    engine.dispose()


def _truncate(engine: sa.Engine) -> None:
    import services.resolve.models  # noqa: F401 -- registers `resolution_decision`

    present = set(sa.inspect(engine).get_table_names())
    names = [t.name for t in Base.metadata.sorted_tables if t.name in present]
    with engine.begin() as conn:
        quoted = ", ".join(f'"{n}"' for n in names)
        conn.execute(sa.text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def pg(pg_engine: sa.Engine) -> Iterator[sessionmaker[Session]]:
    _truncate(pg_engine)
    assets_module._reset_asset_index_cache()
    yield get_sessionmaker(pg_engine)
    assets_module._reset_asset_index_cache()


@pytest.fixture()
def sqlite() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@contextmanager
def _client(maker: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        s = maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    assets_module._reset_asset_index_cache()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        assets_module._reset_asset_index_cache()


def _get(client: TestClient, url: str) -> Any:
    default_limiter.reset()
    resp = client.get(url)
    assert resp.status_code == 200, f"{url}: {resp.status_code} {resp.text[:300]}"
    return resp.json()


# -------------------------------------------------------------------------------------- seed
def _uid(n: int) -> uuid.UUID:
    """Deterministic ids, so both backends hold the same public ids and order rows the same way
    (a uuid orders by bytes on Postgres and by its lower-case text on SQLite: the same order)."""
    return uuid.UUID(int=(0x0190 << 112) | n)


def _licence(s: Session) -> Licence:
    lic = Licence(
        id="lic-pg-parity",
        name="Open Licence",
        reuse_class="open",
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=FIXED,
        classified_by="legal-compliance",
    )
    s.add(lic)
    return lic


def _source(s: Session, lic: Licence) -> Source:
    src = Source(
        id="us.test.pg_parity",
        name="Postgres Parity Source",
        category="generation_queue",
        jurisdiction="US-TX",
        operator="Test Operator",
        url="https://example.org/queue",
        access="bulk_file",
        cadence="weekly",
        licence_id=lic.id,
        publish_state="public",
        manifest_version="2026-09-12",
        manifest_hash="0" * 64,
    )
    s.add(src)
    return src


def _org(s: Session, n: int, name: str) -> Organization:
    oid = _uid(n)
    org = Organization(
        id=oid,
        public_id=public_id("org", oid),
        slug=re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        name_canonical=name,
        name_normalised=name.lower(),
        type="developer",
        country="US",
    )
    s.add(org)
    return org


def _asset(
    s: Session,
    src: Source,
    n: int,
    *,
    geom: tuple[float, float] | None,
    asset_type: str = "power_plant",
    technology: str | None = "wind",
    capacity_mw: float | None = 100.0,
    geom_line: str | None = None,
) -> Asset:
    aid = _uid(n)
    asset = Asset(
        id=aid,
        public_id=public_id("asset", aid),
        slug=f"pg-parity-asset-{n}",
        asset_type=asset_type,
        source_asset_id=str(n),
        name=f"Parity Asset {n}",
        status="operating",
        technology=technology,
        technologies={technology: capacity_mw} if technology and capacity_mw else {},
        capacity_mw=capacity_mw,
        unit_count=1,
        geom=geom,
        geom_line=geom_line,
        attributes={},
        state_code="US-TX",
        county_name="Nolan",
        country="US",
        source_id=src.id,
        source_url=src.url,
        retrieved_at=FIXED,
        licence_id=src.licence_id,
    )
    s.add(asset)
    return asset


def _owner(s: Session, src: Source, n: int, asset: Asset, org: Organization, role: str = "owner") -> None:
    s.add(
        AssetOwner(
            id=_uid(n),
            asset_id=asset.id,
            organization_id=org.id,
            role=role,
            share_pct=100.0 if role == "owner" else None,
            owner_name_raw=org.name_canonical,
            source_id=src.id,
            source_url=src.url,
            retrieved_at=FIXED,
            licence_id=src.licence_id,
        )
    )


def _proposal(
    s: Session, src: Source, n: int, *, geom: tuple[float, float], sponsor: Organization | None
) -> None:
    loc = Location(
        id=_uid(n + 1),
        kind="point",
        geom=geom,
        precision="exact",
        county_name="Travis",
        state_code="US-TX",
        country="US",
        source_id=src.id,
        source_url=src.url,
        retrieved_at=FIXED,
        licence_id=src.licence_id,
    )
    s.add(loc)
    s.flush()  # no ORM relationship orders these inserts, so flush in foreign-key order
    pid = _uid(n)
    s.add(
        Proposal(
            id=pid,
            public_id=public_id("prop", pid),
            slug=f"pg-parity-proposal-{n}",
            kind="storage",
            name_canonical=f"Parity Storage {n}",
            jurisdiction="US-TX",
            lifecycle_state="filed",
            capacity_mw=100.0 + n % 97,
            technology="bess_li_ion",
            publish_state="public",
            published_at=FIXED,
            public_at=FIXED,
            min_reuse_class="open",
            source_count=1,
            sponsor_org_id=sponsor.id if sponsor else None,
            location_id=loc.id,
        )
    )
    s.flush()
    s.add(
        ProposalSource(
            id=_uid(n + 2),
            proposal_id=pid,
            source_id=src.id,
            source_record_id=f"Q{n}",
            source_url=f"{src.url}#{n}",
            retrieved_at=FIXED,
            licence_id=src.licence_id,
            raw={"Queue ID": f"Q{n}"},
            first_seen=FIXED,
            last_seen=FIXED,
        )
    )
    s.add(
        Event(
            id=_uid(n + 3),
            subject_type="proposal",
            subject_id=pid,
            event_type="status_change",
            observed_at=FIXED - dt.timedelta(days=2),
            source_id=src.id,
            source_url=src.url,
            retrieved_at=FIXED,
            licence_id=src.licence_id,
            before={"lifecycle_state": "announced"},
            after={"lifecycle_state": "filed"},
            changed_keys=["lifecycle_state"],
            public_at=FIXED,
            published_at=FIXED,
            idempotency_key=f"pg-parity:{n}",
        )
    )


def _opportunity(s: Session, src: Source, n: int) -> None:
    oid = _uid(n)
    s.add(
        Opportunity(
            id=oid,
            public_id=public_id("opp", oid),
            slug=f"pg-parity-rfp-{n}",
            kind="rfp",
            title=f"Parity RFP {n}",
            jurisdiction="US-AZ",
            technologies=["solar_pv"],
            status="open",
            due_at=dt.datetime(2027, 6, 1, tzinfo=UTC),
            publish_state="public",
            published_at=FIXED,
            public_at=FIXED,
            min_reuse_class="open",
            source_count=1,
        )
    )
    s.flush()
    s.add(
        OpportunitySource(
            id=_uid(n + 1),
            opportunity_id=oid,
            source_id=src.id,
            source_record_id=f"N{n}",
            source_url=f"{src.url}#N{n}",
            retrieved_at=FIXED,
            licence_id=src.licence_id,
            raw={"Notice ID": f"N{n}"},
            first_seen=FIXED,
            last_seen=FIXED,
        )
    )


#: The organisation whose assets span -120..-80 longitude along 35N. Its candidate box is
#: -121..-79 x 34..36; a geography envelope's southern edge bows up to about 35.85N at -100, so
#: the proposal at (-100, 34.3), 78 km south of the middle asset, was dropped on Postgres.
WIDE_ORG = 1
#: The organisation with a pipeline (a line asset) and a plant, several edges per asset.
LINE_ORG = 2
EDGE_PROPOSAL = 500


def seed(maker: sessionmaker[Session], *, cluster_plants: int = 0) -> None:
    """The same rows on either backend. `cluster_plants` adds that many power plants in one small
    area, enough to push `/v1/assets/geo` over its 500-feature split into clusters."""
    with maker() as s:
        lic = _licence(s)
        src = _source(s, lic)
        s.flush()
        wide = _org(s, 10, "Wide Span Energy LLC")
        line_org = _org(s, 11, "Pipeline And Plant Co")
        s.flush()

        # WIDE_ORG: three plants along 35N, one owner edge each.
        for i, lon in enumerate((-120.0, -100.0, -80.0)):
            a = _asset(s, src, 100 + i, geom=(lon, 35.0), technology=("wind", "solar", "gas_ct")[i])
            s.flush()
            _owner(s, src, 200 + i, a, wide)
        _proposal(s, src, EDGE_PROPOSAL, geom=(-100.0, 34.3), sponsor=None)

        # LINE_ORG: a pipeline (line only) and a plant, each with owner and operator edges, so the
        # organisation route has duplicate joins to collapse.
        pipe = _asset(
            s,
            src,
            110,
            geom=None,
            asset_type="gas_pipeline",
            technology=None,
            capacity_mw=None,
            geom_line="MULTILINESTRING((-98.5 30.1, -98.1 30.3, -97.7 30.2))",
        )
        plant = _asset(s, src, 111, geom=(-97.9, 30.05), technology="solar")
        s.flush()
        _owner(s, src, 210, pipe, line_org)
        _owner(s, src, 211, plant, line_org)
        _owner(s, src, 212, pipe, line_org, role="operator")
        _owner(s, src, 213, plant, line_org, role="operator")
        for k, (lon, lat) in enumerate(((-98.2, 30.35), (-97.8, 30.0), (-98.45, 30.25), (-97.0, 30.9))):
            _proposal(s, src, 600 + 10 * k, geom=(lon, lat), sponsor=line_org)

        # Other assets of several types, spread out, for the individual-feature map layer.
        types = (("power_plant", "wind"), ("power_plant", "nuclear"), ("lng_terminal", None))
        for i in range(12):
            asset_type, tech = types[i % 3]
            _asset(
                s,
                src,
                300 + i,
                geom=(round(-104.0 + i * 0.73, 4), round(29.5 + (i % 5) * 0.61, 4)),
                asset_type=asset_type,
                technology=tech,
                capacity_mw=50.0 + i,
            )
        for n in range(3):
            _opportunity(s, src, 800 + 10 * n)
        for i in range(cluster_plants):
            _asset(
                s,
                src,
                10_000 + i,
                geom=(round(-101.0 + (i % 25) * 0.0137, 4), round(33.0 + (i // 25) * 0.0113, 4)),
                technology=("wind", "solar", "gas_ct", "hydro")[i % 4],
                capacity_mw=10.0 + i % 40,
            )
        s.flush()
        _freeze_times(s)
        s.commit()


def _freeze_times(s: Session) -> None:
    """Every defaulted timestamp (`created_at`, `first_seen`, `last_changed`, ...) to `FIXED`."""
    # Inspect through the session's own connection: on in-memory SQLite (one shared connection),
    # inspecting the engine returns that connection to the pool, which rolls the seed back.
    present = set(sa.inspect(s.connection()).get_table_names())
    for table in Base.metadata.sorted_tables:
        cols = [c for c in table.columns if isinstance(c.type, sa.DateTime) and c.default is not None]
        if cols and table.name in present:
            s.execute(table.update().values({c.name: FIXED for c in cols}))


# ------------------------------------------------------------------------------ comparisons
def _cursor(value: str) -> dict[str, Any]:
    """An opaque page cursor, decoded, with its sort value read as a UTC instant. The one
    documented difference in list bodies: a timestamp sort value is encoded without an offset on
    SQLite and with `+00:00` on Postgres (`services/api/pagination.py`), because SQLite returns a
    naive datetime. Each backend decodes its own cursor; `test_list_cursors_...` follows them."""
    payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    v = payload.get("v")
    if isinstance(v, str) and TIMESTAMP.match(v):
        parsed = dt.datetime.fromisoformat(v)
        payload["v"] = (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).isoformat()
    return payload


def _norm(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _cursor(v) if k == "next_cursor" and isinstance(v, str) else _norm(v)
            for k, v in value.items()
            if k not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Every path where `a` and `b` differ; floats equal within 1e-9 (module docstring)."""
    if isinstance(a, float) and isinstance(b, float | int) and not isinstance(b, bool):
        return [] if abs(a - float(b)) <= 1e-9 else [f"{path}: {a!r} != {b!r}"]
    if isinstance(b, float) and isinstance(a, int) and not isinstance(a, bool):
        return _diff(b, a, path)
    if type(a) is not type(b):
        return [f"{path}: {type(a).__name__} {a!r:.80} != {type(b).__name__} {b!r:.80}"]
    if isinstance(a, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: present on one side only")
            else:
                out += _diff(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items != {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b, strict=True)) for d in _diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {a!r:.80} != {b!r:.80}"]


def _timestamps_without_offset(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _timestamps_without_offset(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _timestamps_without_offset(v, f"{path}[{i}]")]
    if isinstance(value, str) and TIMESTAMP.match(value) and not OFFSET.search(value):
        return [f"{path}: {value}"]
    return []


def _both(
    pg: sessionmaker[Session], sqlite: sessionmaker[Session], urls: list[str]
) -> dict[str, tuple[Any, Any]]:
    out: dict[str, list[Any]] = {u: [] for u in urls}
    for maker in (pg, sqlite):
        with _client(maker) as c:
            for u in urls:
                out[u].append(_norm(_get(c, u)))
    return {u: (v[0], v[1]) for u, v in out.items()}


def _assert_same(bodies: dict[str, tuple[Any, Any]]) -> None:
    problems = {u: _diff(p, q)[:8] for u, (p, q) in bodies.items()}
    assert not any(problems.values()), {u: d for u, d in problems.items() if d}


# ------------------------------------------------------------------------------ A1: geo routes
def test_asset_and_plant_geo_individual_features_match_sqlite(
    pg: sessionmaker[Session], sqlite: sessionmaker[Session]
) -> None:
    seed(pg)
    seed(sqlite)
    urls = [
        f"/v1/assets/geo?bbox={WORLD_BBOX}&zoom=4",
        "/v1/assets/geo?bbox=-105,28,-95,33&zoom=7&asset_type=power_plant",
        f"/v1/context/plants/geo?bbox={WORLD_BBOX}&zoom=4",
        f"/v1/assets/geo?organization={public_id('org', _uid(11))}",
    ]
    bodies = _both(pg, sqlite, urls)
    first = bodies[urls[0]][0]["data"]
    assert first["totals"]["clustered"] is False
    kinds = {f["properties"]["feature_kind"] for f in first["features"]}
    assert kinds == {"asset", "asset_line"}
    assert len([f for f in first["features"] if f["properties"]["feature_kind"] == "asset"]) == 16
    _assert_same(bodies)


def test_asset_and_plant_geo_clusters_match_sqlite(
    pg: sessionmaker[Session], sqlite: sessionmaker[Session]
) -> None:
    seed(pg, cluster_plants=600)
    seed(sqlite, cluster_plants=600)
    urls = [
        f"/v1/assets/geo?bbox={WORLD_BBOX}&zoom=6",
        f"/v1/context/plants/geo?bbox={WORLD_BBOX}&zoom=8",
        "/v1/context/plants/geo?bbox=-101.2,32.9,-100.6,33.4&zoom=12",
    ]
    bodies = _both(pg, sqlite, urls)
    for u in urls:
        assert bodies[u][0]["data"]["totals"]["clustered"] is True, u
    clusters = [
        f
        for f in bodies[urls[0]][0]["data"]["features"]
        if f["properties"]["feature_kind"] == "asset_cluster"
    ]
    assert sum(f["properties"]["count"] for f in clusters) >= 600
    _assert_same(bodies)


# --------------------------------------------------------------------- A1: nearby-proposals
def test_organization_nearby_proposals_match_sqlite(
    pg: sessionmaker[Session], sqlite: sessionmaker[Session]
) -> None:
    seed(pg)
    seed(sqlite)
    wide = public_id("org", _uid(10))
    line_org = public_id("org", _uid(11))
    urls = [
        f"/v1/organizations/{wide}/nearby-proposals?radius_km=100",
        f"/v1/organizations/{line_org}/nearby-proposals",
        f"/v1/organizations/{line_org}/nearby-proposals?radius_km=100&role=operator",
        f"/v1/organizations/{line_org}/nearby-proposals?scope=all&asset_type=gas_pipeline",
        f"/v1/assets/{public_id('asset', _uid(110))}/nearby-proposals",
        f"/v1/assets/{public_id('asset', _uid(101))}/nearby-proposals?radius_km=100",
    ]
    bodies = _both(pg, sqlite, urls)

    wide_body = bodies[urls[0]][0]
    assert wide_body["totals"]["assets_considered"] == 3
    # The proposal the geography envelope dropped (module docstring) is found on Postgres.
    assert [row["public_id"] for row in wide_body["data"]] == [public_id("prop", _uid(EDGE_PROPOSAL))]

    line_body = bodies[urls[1]][0]
    # Two assets, four edges: each asset is measured once.
    assert line_body["totals"]["assets_in_scope"] == 2
    assert line_body["totals"]["proposals_within_radius"] == 3
    assert {row["nearest_asset"]["public_id"] for row in line_body["data"]} == {
        public_id("asset", _uid(110)),
        public_id("asset", _uid(111)),
    }
    _assert_same(bodies)


def test_nearby_asset_order_follows_first_owner_edge(
    pg: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap takes assets in order of their first qualifying `asset_owner` edge, the order the
    route documents; the query must express it without `DISTINCT` + a foreign `ORDER BY`."""
    seed(pg)
    with pg() as s:
        org = s.get(Organization, _uid(11))
        assert org is not None
        # Re-point the plant's edges so its first edge sorts before the pipeline's.
        s.execute(sa.update(AssetOwner).where(AssetOwner.id == _uid(211)).values(id=_uid(205)))
        s.commit()
    monkeypatch.setattr(assets_module, "ORG_NEARBY_ASSET_CAP", 1)
    with _client(pg) as c:
        body = _get(c, f"/v1/organizations/{public_id('org', _uid(11))}/nearby-proposals")
    assert body["totals"]["assets_in_scope"] == 1
    assert {row["nearest_asset"]["public_id"] for row in body["data"]} == {public_id("asset", _uid(111))}


# ------------------------------------------------------------------------ A2: list parity
LIST_URLS = [
    "/v1/proposals",
    "/v1/proposals?sort=-capacity_mw&limit=5",
    "/v1/opportunities",
    "/v1/organizations",
    "/v1/assets",
    "/v1/assets?sort=-last_changed&limit=5",
    "/v1/events",
    "/v1/sources",
    f"/v1/organizations/{public_id('org', _uid(11))}/assets",
]


def test_list_endpoints_match_sqlite_and_carry_offsets(
    pg: sessionmaker[Session], sqlite: sessionmaker[Session]
) -> None:
    seed(pg)
    seed(sqlite)
    bodies = _both(pg, sqlite, LIST_URLS)
    for u, (p, q) in bodies.items():
        assert p["data"], f"{u}: empty list proves nothing"
        assert _timestamps_without_offset(p) == [], u
        assert _timestamps_without_offset(q) == [], u
    _assert_same(bodies)


def test_list_cursors_follow_to_the_same_second_page(
    pg: sessionmaker[Session], sqlite: sessionmaker[Session]
) -> None:
    """Each backend's cursor (timestamp-sorted and not) leads to the same next page."""
    seed(pg)
    seed(sqlite)
    pages: list[list[Any]] = []
    for maker in (pg, sqlite):
        with _client(maker) as c:
            bodies = []
            for first in ("/v1/assets?sort=-last_changed&limit=5", "/v1/proposals?limit=2"):
                page1 = _get(c, first)
                cursor = page1["page"]["next_cursor"]
                assert cursor, first
                bodies.append(_norm(_get(c, f"{first}&cursor={cursor}")))
            pages.append(bodies)
    for p, q in zip(pages[0], pages[1], strict=True):
        assert p["data"]
        assert _diff(p, q) == []


def test_proposal_geo_same_features_on_both(pg: sessionmaker[Session], sqlite: sessionmaker[Session]) -> None:
    """A known difference, pinned as one: no `ORDER BY` behind this route, so features and
    licence sources are compared as sets. `last_changed` now carries `Z` on both backends."""
    seed(pg)
    seed(sqlite)
    url = "/v1/proposals/geo?bbox=-99,29.5,-96.5,31&zoom=10"
    p, q = _both(pg, sqlite, [url])[url]

    def as_set(items: list[Any]) -> list[Any]:
        return sorted(items, key=lambda x: repr(sorted(x.items())) if isinstance(x, dict) else repr(x))

    assert len(p["data"]["features"]) == 4
    assert _diff(as_set(p["data"]["features"]), as_set(q["data"]["features"])) == []
    assert _diff(as_set(p["licence_summary"]["sources"]), as_set(q["licence_summary"]["sources"])) == []
    assert _timestamps_without_offset(p) == [] and _timestamps_without_offset(q) == []


# ------------------------------------------------------------------------ A5: event.seq
def _event(n: int, key: str) -> Event:
    return Event(
        subject_type="proposal",
        subject_id=_uid(n),
        event_type="field_changed",
        observed_at=FIXED,
        idempotency_key=key,
        actor_type="pipeline",
    )


def _race(engine: sa.Engine, writers: int, hold: Callable[[int], float]) -> dict[int, str]:
    barrier = threading.Barrier(writers)
    results: dict[int, str] = {}
    maker = get_sessionmaker(engine)

    def run(i: int) -> None:
        with maker() as s:
            e = _event(i, f"pg-race-{i}-{uuid.uuid4()}")
            barrier.wait()
            try:
                s.add(e)
                s.flush()
                time.sleep(hold(i))
                s.commit()
                results[i] = f"ok {e.seq}"
            except Exception as exc:  # recorded, asserted below
                s.rollback()
                results[i] = f"failed {type(getattr(exc, 'orig', exc)).__name__}"

    threads = [threading.Thread(target=run, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_event_writers_all_commit_with_distinct_seq(
    pg: sessionmaker[Session], pg_engine: sa.Engine
) -> None:
    """Before 2026-09-30, 7 of 8 such writers failed with `UniqueViolation event_seq_key`."""
    with pg() as s:  # a stored history first, so a colliding value would be visible
        for i in range(5):
            s.add(_event(i, f"pg-history-{i}"))
        s.commit()
    results = _race(pg_engine, 8, lambda i: 0.4 if i == 0 else 0.02)
    assert all(r.startswith("ok") for r in results.values()), results
    seqs = sorted(int(r.split()[1]) for r in results.values())
    assert len(set(seqs)) == 8 and seqs[0] > 5
    with pg_engine.connect() as conn:
        last_value = conn.execute(sa.text("SELECT last_value FROM event_seq_seq")).scalar()
        stored = conn.execute(sa.text("SELECT max(seq), count(*) FROM event")).one()
    assert stored == (seqs[-1], 13)
    assert last_value == seqs[-1]


def _migration_0027() -> Any:
    path = MIGRATIONS_DIR / "versions" / "0027_event_seq_sequence.py"
    spec = importlib.util.spec_from_file_location("migration_0027", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(engine: sa.Engine) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            _migration_0027().upgrade()


def test_migration_0027_moves_the_sequence_past_stored_values(
    pg: sessionmaker[Session], pg_engine: sa.Engine
) -> None:
    # The pre-0027 state: values written by the old listener, the sequence never called.
    with pg() as s:
        for i, seq in enumerate((1, 2, 981)):
            e = _event(i, f"pg-legacy-{i}")
            e.seq = seq
            s.add(e)
        s.commit()
    with pg_engine.begin() as conn:
        conn.execute(sa.text("SELECT setval('event_seq_seq', 1, false)"))
    with pytest.raises(sa.exc.IntegrityError), pg() as s:
        s.add(_event(9, "pg-collides"))
        s.commit()

    _run_upgrade(pg_engine)
    with pg() as s:
        e = _event(10, "pg-after-0027")
        s.add(e)
        s.commit()
        assert e.seq == 982

    # Never backwards: a sequence already ahead of the data stays where it is.
    with pg_engine.begin() as conn:
        conn.execute(sa.text("SELECT setval('event_seq_seq', 5000, true)"))
    _run_upgrade(pg_engine)
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT last_value FROM event_seq_seq")).scalar() == 5000


def test_migration_0027_on_an_empty_event_table_starts_at_one(
    pg: sessionmaker[Session], pg_engine: sa.Engine
) -> None:
    _run_upgrade(pg_engine)
    with pg() as s:
        e = _event(1, "pg-first")
        s.add(e)
        s.commit()
        assert e.seq == 1


# ------------------------------------------- F5: watermark readers and a seq that commits late
HORIZON_PROPOSAL = 7000


def _horizon_world(maker: sessionmaker[Session]) -> dict[str, Any]:
    """A visible proposal with one committed event, an API-tier webhook endpoint and a saved search
    over events, both at watermark 0."""
    from tests.conftest import make_account, make_user

    with maker() as s:
        src = _source(s, _licence(s))
        s.flush()
        _proposal(s, src, HORIZON_PROPOSAL, geom=(-97.7, 30.3), sponsor=None)
        account = make_account(s, entitlement="api")
        user = make_user(s, account)
        endpoint = WebhookEndpoint(
            public_id=f"whe_horizon_{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            created_by_user_id=user.id,
            url="https://example.com/hook",
            types=["event.published"],
            entity="event",
            query={},
            secret="s",
            watermark_seq=0,
        )
        search = SavedSearch(
            public_id=f"ss_horizon_{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            account_id=account.id,
            name="every event",
            entity="event",
            query={},
            query_hash="0" * 64,
            delivery_mode="immediate",
            channels=["email"],
            watermark_seq=0,
        )
        s.add_all([endpoint, search])
        s.commit()
        return {"source": src.id, "licence": src.licence_id, "endpoint": endpoint.id, "search": search.id}


def _visible_event(world: dict[str, Any], key: str) -> Event:
    return Event(
        subject_type="proposal",
        subject_id=_uid(HORIZON_PROPOSAL),
        event_type="status_change",
        observed_at=FIXED,
        source_id=world["source"],
        source_url="https://example.org/queue",
        retrieved_at=FIXED,
        licence_id=world["licence"],
        before={"lifecycle_state": "filed"},
        after={"lifecycle_state": "studied"},
        changed_keys=["lifecycle_state"],
        public_at=FIXED,
        published_at=FIXED,
        idempotency_key=f"pg-horizon:{key}",
    )


def _read_watermarks(maker: sessionmaker[Session], world: dict[str, Any]) -> dict[str, Any]:
    """Runs every seq-watermark reader once (webhook enqueue, saved-search alerts, social drafts)."""
    from services.alerts.evaluate import evaluate_saved_search
    from services.alerts.webhooks import enqueue_new_deliveries
    from services.social.worker import draft_posts_tick

    with maker() as s:
        enqueue_new_deliveries(s)
        search = s.get(SavedSearch, world["search"])
        assert search is not None
        account = s.get(Account, search.account_id)
        assert account is not None
        alerted = [item.event_seq for item in evaluate_saved_search(s, search, account)]
        s.commit()
        endpoint = s.get(WebhookEndpoint, world["endpoint"])
        assert endpoint is not None
        state: dict[str, Any] = {
            "webhook": endpoint.watermark_seq,
            "delivered": sorted(s.scalars(sa.select(WebhookDelivery.event_seq)).all()),
            "alert": search.watermark_seq,
            "alerted": alerted,
        }
    state["social"] = draft_posts_tick(maker).watermark_seq
    return state


def _event_seqs_since(maker: sessionmaker[Session], since: int) -> list[int]:
    with _client(maker) as client:
        body = _get(client, f"/v1/events?since={since}&sort=seq&limit=50")
    return [row["seq"] for row in body["data"]]


def test_watermark_readers_do_not_skip_an_event_that_commits_after_a_higher_seq(
    pg: sessionmaker[Session],
) -> None:
    """Transaction A takes a seq, B takes the next one and commits, and the readers run while A is
    still open. Before 2026-10-06 every reader moved to B's seq, and A's event, committed a moment
    later, was never delivered, alerted, drafted or listed after `since`."""
    world = _horizon_world(pg)
    first = _read_watermarks(pg, world)
    history = first["webhook"]
    assert history == first["alert"] == first["social"] >= 1

    writer_a = pg()
    try:
        late = _visible_event(world, "a")
        writer_a.add(late)
        writer_a.flush()  # A holds `late.seq`, uncommitted
        with pg() as writer_b:
            early = _visible_event(world, "b")
            writer_b.add(early)
            writer_b.commit()
            early_seq = early.seq
        assert early_seq > late.seq

        during = _read_watermarks(pg, world)
        assert during["webhook"] < late.seq, during
        assert during["alert"] < late.seq, during
        assert during["social"] < late.seq, during
        assert early_seq not in during["delivered"]
        assert _event_seqs_since(pg, history) == []
        writer_a.commit()
        late_seq = late.seq
    finally:
        writer_a.close()

    after = _read_watermarks(pg, world)
    assert {late_seq, early_seq} <= set(after["delivered"])
    assert sorted(after["delivered"]) == sorted(set(after["delivered"]))  # each event once
    assert after["alerted"] == [late_seq, early_seq]
    assert after["webhook"] == after["alert"] == after["social"] == early_seq
    assert _event_seqs_since(pg, history) == [late_seq, early_seq]


def test_stable_event_seq_tracks_open_writers_and_savepoints(
    pg: sessionmaker[Session], pg_engine: sa.Engine
) -> None:
    from services.db.event_horizon import stable_event_seq

    with pg() as s:
        for i in range(3):
            s.add(_event(i, f"pg-horizon-history-{i}"))
        s.commit()
    with pg() as reader:
        assert stable_event_seq(reader) == 3

    a = pg()
    try:
        a.add(_event(10, "pg-horizon-open"))
        a.flush()
        with pg() as b:
            b.add(_event(11, "pg-horizon-other"))
            b.commit()
        with pg() as reader:
            assert stable_event_seq(reader) == 3  # A advertised 3 before it took 4
        # A savepoint that rolls back drops the advertisement with its rows; the next insert
        # advertises again, from the sequence's current value.
        nested = a.begin_nested()
        a.add(_event(12, "pg-horizon-nested"))
        a.flush()
        nested.rollback()
        with pg() as reader:
            assert stable_event_seq(reader) == 3
        a.commit()
    finally:
        a.close()
    with pg() as reader:
        assert stable_event_seq(reader) == 6  # 4 (A), 5 (B), 6 (rolled back, a gap)

    c = pg()
    try:
        nested = c.begin_nested()
        c.add(_event(20, "pg-horizon-nested-only"))
        c.flush()
        nested.rollback()  # the only insert rolled back: lock and flag go together
        with pg() as reader:
            assert stable_event_seq(reader) == 7
        c.add(_event(21, "pg-horizon-after-nested"))
        c.flush()  # takes 8, advertised as 7
        with pg() as reader:
            assert stable_event_seq(reader) == 7
        c.rollback()
    finally:
        c.close()


def test_event_horizon_trigger_is_installed_with_the_reader_constants(pg_engine: sa.Engine) -> None:
    from services.db import event_horizon

    migration_path = MIGRATIONS_DIR / "versions" / "0032_event_horizon_load_marker_subscription_order.py"
    spec = importlib.util.spec_from_file_location("migration_0032", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.LOCK_NAMESPACE, migration.SEQ_BITS) == (
        event_horizon.LOCK_NAMESPACE,
        event_horizon.SEQ_BITS,
    )
    assert migration.EXPECTED_SEQUENCE == f"public.{event_horizon.SEQUENCE}"
    with pg_engine.connect() as conn:
        trigger = conn.execute(
            sa.text("SELECT tgtype FROM pg_trigger WHERE tgname = 'event_seq_horizon' AND NOT tgisinternal")
        ).scalar()
        cache = conn.execute(
            sa.text("SELECT seqcache FROM pg_sequence WHERE seqrelid = 'public.event_seq_seq'::regclass")
        ).scalar()
    assert trigger is not None  # BEFORE, statement-level, INSERT
    assert cache == 1
