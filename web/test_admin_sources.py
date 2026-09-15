"""Tests for `web/admin/sources.py` (Sprint 3 item 3). Fixtures copied verbatim from
`web/test_admin_shell.py` per the task brief (conftest sharing across `web/` test modules is not
available). Both the API-side `admin_sources` router and this task's own web-side `sources`
router are mounted onto the shared apps here, guarded so re-running this module twice (or running
it alongside another admin test module in the same pytest process) never double-registers a route
-- the coordinator's real mount in `services/api/app.py`/`web/app.py` is a later step this suite
does not depend on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import admin_sources as api_admin_sources
from services.api.app import app as api_app
from services.api.conftest import make_open_licence, make_public_source
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import Licence, Source, SourceRun
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user
from web.admin import sources as web_sources
from web.api_client import ApiClient
from web.app import app as web_app

UTC = dt.UTC


def _mount_once(app: Any, path: str, router: Any) -> None:
    if not any(getattr(r, "path", None) == path for r in app.routes):
        app.include_router(router)


_mount_once(api_app, "/admin/v1/sources", api_admin_sources.router)
_mount_once(web_app, "/admin/sources", web_sources.router)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app, follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _sign_in(client: TestClient, db_sessionmaker: sessionmaker[Session], *, role: str) -> None:
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="admin", name="Ops")
        make_user(
            db, account, email=f"{role}@example.com", role=role, password="correct horse battery staple"
        )
        db.commit()
    resp = client.post(
        "/login",
        data={"email": f"{role}@example.com", "password": "correct horse battery staple"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303, resp.text


def make_restricted_licence(session: Session, id_: str = "restricted-lic") -> Licence:
    lic = Licence(
        id=id_,
        name="Restricted Licence",
        reuse_class="restricted",
        gate_flag=True,
        gate_name="G-TEST",
        allows_derived_publication=False,
        allows_raw_publication=False,
        allows_api_redistribution=False,
        allows_bulk_export=False,
        allows_commercial_use=False,
    )
    session.add(lic)
    session.flush()
    return lic


# =========================================================================================== list
def test_list_sources_renders_rows(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        make_public_source(db, lic, id_="us.test.source_a")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/sources")
    assert resp.status_code == 200
    assert "us.test.source_a" in resp.text


def test_list_sources_empty_state_names_the_filter(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        make_public_source(db, lic, id_="us.test.source_b")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/sources?health=failing")
    assert resp.status_code == 200
    assert "No sources match" in resp.text
    assert "health=failing" in resp.text
    assert "Clear all" in resp.text


def test_list_sources_shows_gated_chip(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        restricted = make_restricted_licence(db)
        make_public_source(db, restricted, id_="us.test.gated_source")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/sources")
    assert resp.status_code == 200
    assert "GATED" in resp.text


def test_admin_sources_redirects_anonymous(web_client: TestClient) -> None:
    resp = web_client.get("/admin/sources")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin/sources"


# ========================================================================================= detail
def test_source_detail_shows_licence_evidence(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        make_public_source(db, lic, id_="us.test.source_c")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/sources/us.test.source_c")
    assert resp.status_code == 200
    assert "https://example.org/terms" in resp.text
    assert "legal-compliance" in resp.text


def test_source_detail_404_renders_notice_not_stack_trace(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/sources/does.not.exist")
    assert resp.status_code == 404
    assert "Traceback" not in resp.text


# ---------------------------------------------------------------------------------- pause/resume
def test_pause_flips_source_and_resume_flips_back(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        make_public_source(db, lic, id_="us.test.source_d")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        "/admin/sources/us.test.source_d/pause",
        data={"reason": "maintenance window"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert "flash=Source+paused" in resp.headers["location"]
    detail = web_client.get("/admin/sources/us.test.source_d")
    assert 'chip--neutral">paused' in detail.text

    resp = web_client.post(
        "/admin/sources/us.test.source_d/resume",
        data={"reason": "maintenance done"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail2 = web_client.get("/admin/sources/us.test.source_d")
    assert 'chip--neutral">paused' not in detail2.text


def test_pause_without_origin_is_refused(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        make_public_source(db, lic, id_="us.test.source_e")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post("/admin/sources/us.test.source_e/pause", data={"reason": "x"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------------- publish state
def test_publish_state_to_public_on_restricted_source_renders_422(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        restricted = make_restricted_licence(db, id_="restricted-lic-2")
        source = make_public_source(db, restricted, id_="us.test.source_f")
        source.publish_state = "ingest_only"
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        "/admin/sources/us.test.source_f/publish-state",
        data={"publish_state": "public", "reason": "attempt"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 422
    assert "Licence gate unmet" in resp.text


def test_publish_state_to_public_on_open_source_succeeds(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-2")
        source = make_public_source(db, lic, id_="us.test.source_g")
        source.publish_state = "ingest_only"
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        "/admin/sources/us.test.source_g/publish-state",
        data={"publish_state": "public", "reason": "cleared"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303


# ------------------------------------------------------------------------------------- gate form
def test_gate_form_refused_for_operator(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        restricted = make_restricted_licence(db, id_="restricted-lic-3")
        make_public_source(db, restricted, id_="us.test.source_h")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        "/admin/sources/us.test.source_h/gate",
        data={
            "licence_id": "restricted-lic-3",
            "gate_flag": "false",
            "evidence_url": "https://example.org/e",
            "evidence_retrieved_at": "2026-09-01",
            "classified_by": "legal-compliance",
            "reason": "trying anyway",
        },
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 403


def test_gate_form_clears_gate_for_legal(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        restricted = make_restricted_licence(db, id_="restricted-lic-4")
        make_public_source(db, restricted, id_="us.test.source_i")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="legal")
    resp = web_client.post(
        "/admin/sources/us.test.source_i/gate",
        data={
            "licence_id": "restricted-lic-4",
            "gate_flag": "false",
            "evidence_url": "https://example.org/e",
            "evidence_retrieved_at": "2026-09-01",
            "classified_by": "legal-compliance",
            "reason": "cleared after review",
        },
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303


# ------------------------------------------------------------------------------------- run now
def test_run_now_renders_queue_unavailable_cleanly(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-3")
        source = make_public_source(db, lic, id_="us.test.source_j")
        source.implemented = True
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        "/admin/sources/us.test.source_j/run",
        data={"reason": "manual check"},
        headers={"origin": "http://testserver"},
    )
    # No Postgres/Procrastinate in this sandbox: `QueuedSourceRunner` wraps the failure as a fixed
    # 503, rendered through `_notice.html`, never a stack trace.
    assert resp.status_code == 503
    assert "Traceback" not in resp.text


class _FakeSourceRunner:
    def enqueue(self, db: Session, source: Source, *, trigger: str, requested_by: Any) -> SourceRun:
        run = SourceRun(
            source_id=source.id,
            trigger=trigger,
            started_at=dt.datetime.now(UTC),
            status="running",
            egress_class=source.egress,
        )
        db.add(run)
        db.flush()
        return run


def test_run_now_succeeds_with_a_fake_runner(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-4")
        source = make_public_source(db, lic, id_="us.test.source_k")
        source.implemented = True
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    api_app.dependency_overrides[api_admin_sources.get_source_runner] = lambda: _FakeSourceRunner()
    try:
        resp = web_client.post(
            "/admin/sources/us.test.source_k/run",
            data={"reason": "manual check"},
            headers={"origin": "http://testserver"},
        )
        assert resp.status_code == 303
        assert "flash=Run+queued" in resp.headers["location"]
    finally:
        del api_app.dependency_overrides[api_admin_sources.get_source_runner]


# ============================================================================== add curated issuer
def test_add_issuer_creates_a_source(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        make_open_licence(db, id_="open-lic-5")
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        "/admin/sources/new",
        data={
            "source_id": "us.test.curated_issuer",
            "name": "Test Issuer",
            "operator": "Test Co",
            "url": "https://example.org/issuer",
            "access": "html",
            "cadence": "weekly",
            "licence_id": "open-lic-5",
            "reason": "adding curated issuer",
        },
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert "/admin/sources/us.test.curated_issuer" in resp.headers["location"]


# =================================================================================== source runs
def test_source_runs_list_and_detail(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-6")
        source = make_public_source(db, lic, id_="us.test.source_l")
        source.implemented = True
        db.commit()
    _sign_in(web_client, db_sessionmaker, role="operator")
    api_app.dependency_overrides[api_admin_sources.get_source_runner] = lambda: _FakeSourceRunner()
    try:
        run_resp = web_client.post(
            "/admin/sources/us.test.source_l/run",
            data={"reason": "seed a run"},
            headers={"origin": "http://testserver"},
        )
        assert run_resp.status_code == 303
    finally:
        del api_app.dependency_overrides[api_admin_sources.get_source_runner]

    list_resp = web_client.get("/admin/source-runs")
    assert list_resp.status_code == 200
    assert "us.test.source_l" in list_resp.text

    filtered_empty = web_client.get("/admin/source-runs?status=failed")
    assert filtered_empty.status_code == 200
    assert "No runs match" in filtered_empty.text
    assert "status=failed" in filtered_empty.text
