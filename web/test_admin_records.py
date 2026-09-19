"""Tests for `web/admin/records.py` (Sprint 3 item 3): records, resolution and extraction review.
Fixtures copied verbatim from `web/test_admin_shell.py` (conftest sharing across `web/` test
modules is not available). Both the API-side `admin_records` router and this task's own web-side
`records` router are mounted onto the shared apps here, guarded so re-running this module (or
running it alongside `web/test_admin_sources.py` in the same pytest process) never
double-registers a route.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import admin_records as api_admin_records
from services.api.app import app as api_app
from services.api.conftest import (
    make_event,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import Extraction
from services.db.session import get_engine, get_sessionmaker, init_db
from services.resolve.models import ResolutionDecision
from tests.conftest import make_account, make_user
from web.admin import records as web_records
from web.api_client import ApiClient
from web.app import app as web_app

UTC = dt.UTC


def _mount_once(app: Any, path: str, router: Any) -> None:
    if not any(getattr(r, "path", None) == path for r in app.routes):
        app.include_router(router)


_mount_once(api_app, "/admin/v1/proposals/{public_id}", api_admin_records.router)
_mount_once(web_app, "/admin/records", web_records.router)


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


def _hidden_value(html: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}"\s+value="([^"]*)"', html)
    assert match, f"no hidden field named {name!r} in the response"
    return match.group(1)


def _api_get(web_client: TestClient, path: str) -> dict[str, Any]:
    """Reads an `/admin/v1` endpoint directly, reusing the session cookie the web client already
    carries after `_sign_in`, to assert on the true API state without re-parsing HTML."""
    direct = TestClient(api_app)
    direct.cookies.set("session", web_client.cookies.get("session"))
    resp = direct.get(path)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


# ======================================================================================= lookup
def test_records_lookup_redirects_by_prefix(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        source = make_public_source(db, lic, id_="us.test.recsrc_a")
        proposal = make_visible_proposal(db, source, public_id_suffix="1")
        db.commit()
        public_id = proposal.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get(f"/admin/records/lookup?public_id={public_id}")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/records/proposals/{public_id}"


def test_records_lookup_rejects_unrecognised_prefix(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/records/lookup?public_id=nope_123")
    assert resp.status_code == 400
    assert "Unrecognised id" in resp.text


def test_admin_records_redirects_anonymous(web_client: TestClient) -> None:
    resp = web_client.get("/admin/records")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin/records"


# ==================================================================================== proposals
def test_proposal_detail_renders_sources_and_events(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-1")
        source = make_public_source(db, lic, id_="us.test.recsrc_b")
        proposal = make_visible_proposal(db, source, public_id_suffix="2")
        make_event(db, proposal, source, event_type="status_change")
        db.commit()
        public_id = proposal.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get(f"/admin/records/proposals/{public_id}")
    assert resp.status_code == 200
    assert "us.test.recsrc_b" in resp.text
    assert "status_change" in resp.text


def test_proposal_detail_404_for_operator(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/records/proposals/prop_00000000000")
    assert resp.status_code == 404
    assert "Traceback" not in resp.text


def test_edit_proposal_changes_the_name(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-2")
        source = make_public_source(db, lic, id_="us.test.recsrc_c")
        proposal = make_visible_proposal(db, source, public_id_suffix="3")
        db.commit()
        public_id = proposal.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        f"/admin/records/proposals/{public_id}/edit",
        data={"name_canonical": "Renamed Storage Project", "reason": "typo fix"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail = web_client.get(f"/admin/records/proposals/{public_id}")
    assert "Renamed Storage Project" in detail.text


def test_edit_proposal_without_origin_is_refused(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-3")
        source = make_public_source(db, lic, id_="us.test.recsrc_d")
        proposal = make_visible_proposal(db, source, public_id_suffix="4")
        db.commit()
        public_id = proposal.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        f"/admin/records/proposals/{public_id}/edit",
        data={"name_canonical": "x", "reason": "y"},
    )
    assert resp.status_code == 403


def test_proposal_publish_state_updates(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-4")
        source = make_public_source(db, lic, id_="us.test.recsrc_e")
        proposal = make_visible_proposal(db, source, public_id_suffix="5")
        db.commit()
        public_id = proposal.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        f"/admin/records/proposals/{public_id}/publish-state",
        data={"publish_state": "unpublished", "reason": "takedown drill"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail = web_client.get(f"/admin/records/proposals/{public_id}")
    assert 'chip--neutral">unpublished' in detail.text


# -------------------------------------------------------------------------------- merge/unmerge
def test_merge_preview_then_apply_merges(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-5")
        source = make_public_source(db, lic, id_="us.test.recsrc_f")
        surviving = make_visible_proposal(db, source, public_id_suffix="6")
        absorbed = make_visible_proposal(db, source, public_id_suffix="7")
        absorbed.capacity_mw = surviving.capacity_mw  # remove the two comparable-field conflicts
        absorbed.name_canonical = surviving.name_canonical
        db.commit()
        surviving_id = surviving.public_id
        absorbed_id = absorbed.public_id

    _sign_in(web_client, db_sessionmaker, role="operator")

    preview = web_client.post(
        f"/admin/records/proposals/{surviving_id}/merge",
        data={"step": "preview", "absorb_public_id": absorbed_id},
        headers={"origin": "http://testserver"},
    )
    assert preview.status_code == 200
    assert f"absorbing {absorbed_id}" in preview.text
    assert "No conflicting fields." in preview.text
    preview_token = _hidden_value(preview.text, "preview_token")

    apply_resp = web_client.post(
        f"/admin/records/proposals/{surviving_id}/merge",
        data={
            "step": "apply",
            "absorb_public_id": absorbed_id,
            "preview_token": preview_token,
            "reason": "confirmed duplicate",
        },
        headers={"origin": "http://testserver"},
    )
    assert apply_resp.status_code == 303
    assert "flash=Merge+applied" in apply_resp.headers["location"]

    absorbed_after = _api_get(web_client, f"/admin/v1/proposals/{absorbed_id}")["data"]
    assert absorbed_after["publish_state"] == "unpublished"

    surviving_after = _api_get(web_client, f"/admin/v1/proposals/{surviving_id}")["data"]
    merge_events = [e for e in surviving_after["events"] if e["event_type"] == "merged"]
    assert len(merge_events) == 1
    merge_event_id = merge_events[0]["id"]

    unmerge_resp = web_client.post(
        f"/admin/records/proposals/{surviving_id}/unmerge",
        data={"merge_event_id": merge_event_id, "reason": "wrong call"},
        headers={"origin": "http://testserver"},
    )
    assert unmerge_resp.status_code == 303
    assert "flash=Merge+reversed" in unmerge_resp.headers["location"]

    restored = _api_get(web_client, f"/admin/v1/proposals/{absorbed_id}")["data"]
    assert restored["merged_into"] is None


# ================================================================================= opportunities
def test_opportunity_detail_and_edit(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-6")
        source = make_public_source(db, lic, id_="us.test.recsrc_g")
        opportunity = make_visible_opportunity(db, source, public_id_suffix="1")
        db.commit()
        public_id = opportunity.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    detail = web_client.get(f"/admin/records/opportunities/{public_id}")
    assert detail.status_code == 200
    assert "us.test.recsrc_g" in detail.text

    resp = web_client.post(
        f"/admin/records/opportunities/{public_id}/edit",
        data={"title": "Renamed RFP", "reason": "typo fix"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail2 = web_client.get(f"/admin/records/opportunities/{public_id}")
    assert "Renamed RFP" in detail2.text


def test_opportunity_publish_state_updates(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-7")
        source = make_public_source(db, lic, id_="us.test.recsrc_h")
        opportunity = make_visible_opportunity(db, source, public_id_suffix="2")
        db.commit()
        public_id = opportunity.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        f"/admin/records/opportunities/{public_id}/publish-state",
        data={"publish_state": "unpublished", "reason": "takedown drill"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail = web_client.get(f"/admin/records/opportunities/{public_id}")
    assert 'chip--neutral">unpublished' in detail.text


# ================================================================================= organizations
def test_organization_detail_and_edit(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        org = make_org(db, name="Acme Developer LLC")
        db.commit()
        public_id = org.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    detail = web_client.get(f"/admin/records/organizations/{public_id}")
    assert detail.status_code == 200
    assert "Acme Developer LLC" in detail.text

    resp = web_client.post(
        f"/admin/records/organizations/{public_id}/edit",
        data={"name_canonical": "Acme Developer Renamed LLC", "reason": "name change"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail2 = web_client.get(f"/admin/records/organizations/{public_id}")
    assert "Acme Developer Renamed LLC" in detail2.text


def test_organization_edit_without_origin_is_refused(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        org = make_org(db, name="No Origin LLC")
        db.commit()
        public_id = org.public_id
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        f"/admin/records/organizations/{public_id}/edit",
        data={"name_canonical": "x", "reason": "y"},
    )
    assert resp.status_code == 403


# =================================================================================== resolution
def test_resolution_list_and_decide_same_merges(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-8")
        source = make_public_source(db, lic, id_="us.test.recsrc_i")
        left = make_visible_proposal(db, source, public_id_suffix="8")
        right = make_visible_proposal(db, source, public_id_suffix="9")
        decision = ResolutionDecision(
            left_proposal_id=left.id,
            right_proposal_id=right.id,
            cluster_key="k1",
            score=72.5,
            rationale="same name, same county",
            gate_reason="below auto-merge threshold",
        )
        db.add(decision)
        db.commit()
        decision_id = decision.id
        left_public_id = left.public_id

    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/resolution")
    assert resp.status_code == 200
    assert "72.5" in resp.text

    from services.ids import public_id as make_public_id

    candidate_id = make_public_id("rc", decision_id)
    decide_resp = web_client.post(
        f"/admin/resolution/{candidate_id}/decide",
        data={"decision": "same", "surviving_public_id": left_public_id, "reason": "confirmed duplicate"},
        headers={"origin": "http://testserver"},
    )
    assert decide_resp.status_code == 303
    assert "flash=Decision+recorded" in decide_resp.headers["location"]

    decided_list = web_client.get("/admin/resolution?status=decided")
    assert decided_list.status_code == 200
    assert "same" in decided_list.text


def test_resolution_empty_state(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/resolution")
    assert resp.status_code == 200
    assert "No pending candidates" in resp.text


# =================================================================================== extractions
def test_extractions_list_accept_and_reject(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as db:
        lic = make_open_licence(db, id_="open-lic-rec-9")
        source = make_public_source(db, lic, id_="us.test.recsrc_j")
        proposal = make_visible_proposal(db, source, public_id_suffix="10")
        accept_extraction = Extraction(
            public_id="ext_test_accept_1",
            subject_type="proposal",
            subject_id=proposal.id,
            purpose="extract",
            field_path="capacity_mw",
            payload={"capacity_mw": 250.0},
            confidence=0.55,
            citations=[{"document_id": None, "quote": "250 MW"}],
            source_id=source.id,
            licence_id=lic.id,
        )
        reject_extraction = Extraction(
            public_id="ext_test_reject_1",
            subject_type="proposal",
            subject_id=proposal.id,
            purpose="extract",
            field_path="iso",
            payload={"iso": "ERCOT"},
            confidence=0.4,
            citations=[{"document_id": None, "quote": "ERCOT queue"}],
            source_id=source.id,
            licence_id=lic.id,
        )
        db.add_all([accept_extraction, reject_extraction])
        db.commit()
        proposal_id = proposal.public_id

    _sign_in(web_client, db_sessionmaker, role="operator")
    listing = web_client.get("/admin/extractions")
    assert listing.status_code == 200
    assert "capacity_mw" in listing.text
    assert "iso" in listing.text

    accept_resp = web_client.post(
        "/admin/extractions/ext_test_accept_1/accept",
        data={"reason": "confirmed against source"},
        headers={"origin": "http://testserver"},
    )
    assert accept_resp.status_code == 303
    assert "flash=Extraction+accepted" in accept_resp.headers["location"]

    updated_proposal = _api_get(web_client, f"/admin/v1/proposals/{proposal_id}")["data"]
    assert updated_proposal["capacity_mw"] == 250.0

    reject_resp = web_client.post(
        "/admin/extractions/ext_test_reject_1/reject",
        data={"reason": "low confidence, unverifiable"},
        headers={"origin": "http://testserver"},
    )
    assert reject_resp.status_code == 303
    assert "flash=Extraction+rejected" in reject_resp.headers["location"]

    still_proposal_iso = _api_get(web_client, f"/admin/v1/proposals/{proposal_id}")["data"]
    assert still_proposal_iso["iso"] != "ERCOT"


def test_extractions_empty_state(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/extractions?status=accepted")
    assert resp.status_code == 200
    assert "No extractions are awaiting review" in resp.text or "No extractions match" in resp.text
