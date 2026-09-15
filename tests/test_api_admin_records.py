"""Tests for `services/api/admin_records.py` — record editing, publish/unpublish, merge/unmerge,
resolution review and extraction review under `/admin/v1` (Sprint 3 item 3).

`admin_records.router` is mounted by the coordinator onto `services.api.app.app`; this suite
builds its own standalone `FastAPI` app carrying only that router plus the `ProblemError` handler
instead (mirrors `services/crm/test_router.py`'s "option B", one of this task's read-only files —
same rationale: three other backend agents mount their own `/admin/v1` routers concurrently, so
this suite never depends on load order or on the shared app already carrying this router).

`login`/`make_account`/`make_user` from `tests/conftest.py` and entity factories
(`make_open_licence`, `make_public_source`, `make_org`, `make_visible_proposal`, ...) from
`services/api/conftest.py` are plain functions, not fixtures bound to that module's own `client`,
so reusing them against this file's own `client`/`db` fixtures is safe. `assert_valid`/`spec` are
imported directly from `tests/test_api_contract.py` per this task's read list and used as an
ordinary pytest fixture (`spec`) the same way that module's own tests do.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.admin_records import router
from services.api.conftest import (
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.api.errors import ProblemError, problem_exception_handler
from services.db.models import Document, Event, Extraction, Licence, ModelCall, Opportunity, Proposal
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.resolve.models import ResolutionDecision
from tests.conftest import login, make_account, make_user
from tests.test_api_contract import assert_valid

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "api" / "openapi.yaml"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ProblemError, problem_exception_handler)
    app.include_router(router)
    return app


@pytest.fixture(scope="module")
def spec() -> dict:
    # Same file `tests/test_api_contract.py`'s own `spec` fixture loads; reloaded locally rather
    # than importing that fixture object, which triggers a spurious ruff F811 across every test
    # function that also names its `spec` parameter (see that module for `assert_valid`, reused
    # directly above).
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Session:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def client(db_sessionmaker: sessionmaker[Session]) -> TestClient:
    def _override():
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = _build_app()
    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------------------------ factories
def admin_login(client, db, *, role: str = "operator", email: str | None = None):
    account = make_account(db, entitlement="admin", name="Ops")
    user = make_user(db, account, email=email or f"{role}@example.com", role=role)
    db.commit()
    login(client, db, user)
    return user


def make_document(db, source, licence, *, suffix: str = "1") -> Document:
    doc = Document(
        public_id="",
        subject_type="none",
        source_id=source.id,
        source_url=f"{source.url}/doc{suffix}",
        retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        licence_id=licence.id,
        title=f"Test filing {suffix}",
        doc_type="filing",
        storage_policy="link_only",
    )
    db.add(doc)
    db.flush()
    doc.public_id = public_id("doc", doc.id)
    db.flush()
    return doc


def make_extraction(
    db,
    proposal: Proposal,
    document: Document,
    source,
    licence: Licence,
    *,
    field_path: str = "lifecycle_state",
    value: object = "permitted",
    status: str = "proposed",
) -> Extraction:
    ext = Extraction(
        public_id="",
        document_id=document.id,
        subject_type="proposal",
        subject_id=proposal.id,
        purpose="extract",
        field_path=field_path,
        payload={field_path: value},
        confidence=0.62,
        citations=[{"document_id": document.public_id, "page": 3}],
        status=status,
        source_id=source.id,
        licence_id=licence.id,
    )
    db.add(ext)
    db.flush()
    ext.public_id = public_id("ext", ext.id)
    db.flush()
    return ext


def make_restricted_derived_licence(db, id_: str = "restricted-derived") -> Licence:
    lic = Licence(
        id=id_,
        name="Restricted Licence",
        reuse_class="restricted",
        allows_derived_publication=False,
        allows_raw_publication=False,
        gate_flag=True,
        evidence_url="https://example.org/terms3",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def make_resolution_decision(
    db, left: Proposal, right: Proposal, *, score: float = 68.0, status: str = "proposed"
) -> ResolutionDecision:
    left_id, right_id = sorted((left.id, right.id), key=str)
    decision = ResolutionDecision(
        left_proposal_id=left_id,
        right_proposal_id=right_id,
        cluster_key="test-cluster",
        score=score,
        rationale="name and location similarity below the auto-merge threshold",
        gate_reason=f"min pairwise score {score:.1f} below threshold 75",
        status=status,
        extra={"name_similarity": 0.81, "location_similarity": 0.74},
    )
    db.add(decision)
    db.flush()
    return decision


def _make_proposal_pair(db, source, *, suffix_a: str, suffix_b: str):
    left = make_visible_proposal(db, source, public_id_suffix=suffix_a)
    right = make_visible_proposal(db, source, public_id_suffix=suffix_b)
    return left, right


# ============================================================================ GET/PATCH proposal
def test_admin_get_proposal_requires_admin_session(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="1")
    db.commit()

    resp = client.get(f"/admin/v1/proposals/{proposal.public_id}")
    assert resp.status_code == 401


def test_admin_get_proposal_forbidden_for_non_operator_role(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="2")
    db.commit()
    admin_login(client, db, role="viewer")

    resp = client.get(f"/admin/v1/proposals/{proposal.public_id}")
    assert resp.status_code == 403


def test_admin_get_proposal_not_found(client, db):
    admin_login(client, db)
    resp = client.get("/admin/v1/proposals/prop_0000000000")
    assert resp.status_code == 404


def test_admin_get_proposal_happy_path_shows_sources_and_events(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="3")
    db.commit()
    admin_login(client, db)

    resp = client.get(f"/admin/v1/proposals/{proposal.public_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminProposalDetailResponse", body)
    data = body["data"]
    assert data["publish_state"] == "public"
    assert len(data["sources"]) == 1
    assert data["sources"][0]["source_record_id"] == "Q3"  # never withheld from admin


def test_admin_update_proposal_missing_reason_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="4")
    db.commit()
    admin_login(client, db)

    resp = client.patch(f"/admin/v1/proposals/{proposal.public_id}", json={"lifecycle_state": "permitted"})
    assert resp.status_code == 400


def test_admin_update_proposal_bad_vocab_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="5")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/proposals/{proposal.public_id}", json={"kind": "not-a-kind", "reason": "x"}
    )
    assert resp.status_code == 400


def test_admin_update_proposal_unknown_sponsor_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="6")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/proposals/{proposal.public_id}",
        json={"sponsor_org_id": "org_0000000000", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_update_proposal_happy_path_writes_audit_event_and_override(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="7")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/proposals/{proposal.public_id}",
        json={"lifecycle_state": "permitted", "reason": "permit issued per filing #4412"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminProposalDetailResponse", body)
    data = body["data"]
    assert data["lifecycle_state"] == "permitted"
    assert data["overrides"]["lifecycle_state"]["value"] == "permitted"
    assert data["last_admin_event_id"]

    db.expire_all()
    ev = db.query(Event).filter_by(event_type="admin_edit", subject_id=proposal.id).one()
    assert ev.actor_type == "user"
    assert ev.reason == "permit issued per filing #4412"
    assert ev.before == {"lifecycle_state": "filed"}
    assert ev.after == {"lifecycle_state": "permitted"}


def test_admin_update_proposal_clear_overrides(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="8")
    db.commit()
    admin_login(client, db)
    url = f"/admin/v1/proposals/{proposal.public_id}"

    first = client.patch(url, json={"lifecycle_state": "permitted", "reason": "first edit"})
    assert first.status_code == 200
    assert "lifecycle_state" in first.json()["data"]["overrides"]

    second = client.patch(
        url,
        json={
            "lifecycle_state": "contracted",
            "reason": "second edit",
            "clear_overrides": ["lifecycle_state"],
        },
    )
    assert second.status_code == 200
    assert "lifecycle_state" not in second.json()["data"]["overrides"]


def test_admin_update_proposal_sets_manual_location(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="9")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/proposals/{proposal.public_id}",
        json={
            "location": {"county_fips": "48453", "state_code": "US-TX", "country": "US"},
            "reason": "corrected county after site visit",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminProposalDetailResponse", body)
    assert body["data"]["location"]["county_fips"] == "48453"
    assert body["data"]["location"]["geocoder"] == "manual"

    db.expire_all()
    ev = db.query(Event).filter_by(event_type="admin_edit", subject_id=proposal.id).one()
    assert "location" in ev.after


def test_admin_update_proposal_no_change_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="17", lifecycle_state="filed")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/proposals/{proposal.public_id}",
        json={"lifecycle_state": "filed", "reason": "no-op"},
    )
    assert resp.status_code == 400


# ========================================================================= GET/PATCH opportunity
def test_admin_get_opportunity_happy_path(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="1")
    db.commit()
    admin_login(client, db)

    resp = client.get(f"/admin/v1/opportunities/{opportunity.public_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminOpportunityDetailResponse", body)
    assert body["data"]["publish_state"] == "public"


def test_admin_get_opportunity_not_found(client, db):
    admin_login(client, db)
    resp = client.get("/admin/v1/opportunities/opp_0000000000")
    assert resp.status_code == 404


def test_admin_update_opportunity_missing_reason_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="2")
    db.commit()
    admin_login(client, db)

    resp = client.patch(f"/admin/v1/opportunities/{opportunity.public_id}", json={"status": "closed"})
    assert resp.status_code == 400


def test_admin_update_opportunity_bad_vocab_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="3")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/opportunities/{opportunity.public_id}",
        json={"status": "not-a-status", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_update_opportunity_happy_path_writes_audit_event(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="4")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/opportunities/{opportunity.public_id}",
        json={"status": "closed", "reason": "issuer confirmed award made"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminOpportunityDetailResponse", body)
    assert body["data"]["status"] == "closed"

    db.expire_all()
    ev = db.query(Event).filter_by(event_type="admin_edit", subject_id=opportunity.id).one()
    assert ev.before == {"status": "open"}
    assert ev.after == {"status": "closed"}


# =========================================================================== PATCH organization
def test_admin_update_organization_bad_vocab_is_400(client, db):
    org = make_org(db)
    db.commit()
    admin_login(client, db)

    resp = client.patch(f"/admin/v1/organizations/{org.public_id}", json={"type": "nope", "reason": "x"})
    assert resp.status_code == 400


def test_admin_update_organization_happy_path(client, db, spec):
    org = make_org(db)
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/organizations/{org.public_id}",
        json={"is_curated_issuer": True, "reason": "confirmed on the curated issuer list"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "OrganizationDetailResponse", body)
    assert body["data"]["is_curated_issuer"] is True

    db.expire_all()
    ev = db.query(Event).filter_by(event_type="admin_edit", subject_id=org.id).one()
    assert ev.before == {"is_curated_issuer": False}
    assert ev.after == {"is_curated_issuer": True}


def test_admin_update_organization_not_found(client, db):
    admin_login(client, db)
    resp = client.patch("/admin/v1/organizations/org_0000000000", json={"reason": "x", "website": None})
    assert resp.status_code == 404


# ================================================================================ publish-state
def test_admin_set_publish_state_missing_reason_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="60")
    db.commit()
    admin_login(client, db)

    resp = client.put(
        f"/admin/v1/records/proposals/{proposal.public_id}/publish-state",
        json={"publish_state": "unpublished"},
    )
    assert resp.status_code == 400


def test_admin_set_publish_state_bad_vocab_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="61")
    db.commit()
    admin_login(client, db)

    resp = client.put(
        f"/admin/v1/records/proposals/{proposal.public_id}/publish-state",
        json={"publish_state": "banana", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_set_publish_state_not_found(client, db):
    admin_login(client, db)
    resp = client.put(
        "/admin/v1/records/proposals/prop_0000000000/publish-state",
        json={"publish_state": "public", "reason": "x"},
    )
    assert resp.status_code == 404


def test_admin_set_publish_state_organizations_unsupported(client, db):
    org = make_org(db)
    db.commit()
    admin_login(client, db)

    resp = client.put(
        f"/admin/v1/records/organizations/{org.public_id}/publish-state",
        json={"publish_state": "public", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_set_publish_state_gate_unmet_for_restricted_source(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="62")
    proposal.min_reuse_class = "restricted"
    db.commit()
    admin_login(client, db)

    resp = client.put(
        f"/admin/v1/records/proposals/{proposal.public_id}/publish-state",
        json={"publish_state": "public", "reason": "x"},
    )
    assert resp.status_code == 422


def test_admin_set_publish_state_happy_path_audits(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="63")
    db.commit()
    admin_login(client, db)

    resp = client.put(
        f"/admin/v1/records/proposals/{proposal.public_id}/publish-state",
        json={"publish_state": "unpublished", "reason": "duplicate, superseded by prop_9999"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "RecordPublishStateResponse", body)
    assert body["data"]["publish_state"] == "unpublished"

    db.expire_all()
    assert db.get(Proposal, proposal.id).publish_state == "unpublished"
    ev = db.query(Event).filter_by(event_type="unpublished", subject_id=proposal.id).one()
    assert ev.before == {"publish_state": "public"}
    assert ev.after == {"publish_state": "unpublished", "takedown": False}


def test_admin_set_publish_state_takedown_nulls_event_visibility(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="64")
    db.commit()
    admin_login(client, db)

    resp = client.put(
        f"/admin/v1/records/proposals/{proposal.public_id}/publish-state",
        json={"publish_state": "unpublished", "reason": "owner requested takedown", "takedown": True},
    )
    assert resp.status_code == 200

    db.expire_all()
    events = db.query(Event).filter_by(subject_type="proposal", subject_id=proposal.id).all()
    assert events, "expected at least the proposal's seed event and the new admin event"
    for ev in events:
        assert ev.published_at is None
        assert ev.public_at is None


# ===================================================================================== merge
def test_admin_merge_same_record_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="70")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{proposal.public_id}/merge",
        json={"absorb_public_id": proposal.public_id, "preview": True},
    )
    assert resp.status_code == 409


def test_admin_merge_apply_without_preview_token_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    survivor, absorbed = _make_proposal_pair(db, source, suffix_a="71", suffix_b="72")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={"absorb_public_id": absorbed.public_id, "preview": False, "reason": "dup"},
    )
    assert resp.status_code == 400


def test_admin_merge_already_absorbed_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    a, b = _make_proposal_pair(db, source, suffix_a="73", suffix_b="74")
    c = make_visible_proposal(db, source, public_id_suffix="75")
    db.commit()
    admin_login(client, db)

    preview = client.post(
        f"/admin/v1/proposals/{a.public_id}/merge", json={"absorb_public_id": b.public_id, "preview": True}
    ).json()
    apply_resp = client.post(
        f"/admin/v1/proposals/{a.public_id}/merge",
        json={
            "absorb_public_id": b.public_id,
            "preview": False,
            "preview_token": preview["data"]["preview_token"],
            "reason": "dup",
        },
    )
    assert apply_resp.status_code == 200

    resp = client.post(
        f"/admin/v1/proposals/{c.public_id}/merge",
        json={"absorb_public_id": b.public_id, "preview": True},
    )
    assert resp.status_code == 409


def test_admin_merge_preview_then_apply_survivor_keeps_sources_absorbed_restorable(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    survivor, absorbed = _make_proposal_pair(db, source, suffix_a="76", suffix_b="77")
    db.commit()
    admin_login(client, db)

    preview_resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={"absorb_public_id": absorbed.public_id, "preview": True},
    )
    assert preview_resp.status_code == 200
    preview_body = preview_resp.json()
    assert_valid(spec, "MergeResponse", preview_body)
    assert preview_body["data"]["preview"] is True
    token = preview_body["data"]["preview_token"]
    assert token
    conflicts = preview_body["data"]["conflicts"]
    assert any(c["field"] == "name_canonical" for c in conflicts)

    apply_resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={
            "absorb_public_id": absorbed.public_id,
            "preview": False,
            "preview_token": token,
            "reason": "duplicate queue rows for the same project",
        },
    )
    assert apply_resp.status_code == 200
    apply_body = apply_resp.json()
    assert_valid(spec, "MergeResponse", apply_body)
    assert apply_body["data"]["preview"] is False
    event_id = apply_body["data"]["event_id"]
    assert event_id
    assert apply_body["data"]["moved"]["proposal_source_ids"]

    db.expire_all()
    survivor_row = db.get(Proposal, survivor.id)
    absorbed_row = db.get(Proposal, absorbed.id)
    assert survivor_row.source_count == 2
    assert absorbed_row.merged_into_id == survivor_row.id
    assert absorbed_row.publish_state == "unpublished"

    merge_event = db.query(Event).filter_by(event_type="merged", subject_id=survivor_row.id).one()
    assert merge_event.actor_user_id is not None
    assert merge_event.reason == "duplicate queue rows for the same project"

    unmerge_resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/unmerge",
        json={"merge_event_id": event_id, "reason": "wrong merge, reverting"},
    )
    assert unmerge_resp.status_code == 200
    unmerge_body = unmerge_resp.json()
    assert_valid(spec, "UnmergeResponse", unmerge_body)

    db.expire_all()
    absorbed_after = db.get(Proposal, absorbed.id)
    survivor_after = db.get(Proposal, survivor.id)
    assert absorbed_after.merged_into_id is None
    assert absorbed_after.publish_state == "public"
    assert survivor_after.source_count == 1


def test_admin_merge_stale_preview_token_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    survivor, absorbed = _make_proposal_pair(db, source, suffix_a="78", suffix_b="79")
    db.commit()
    admin_login(client, db)

    preview = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={"absorb_public_id": absorbed.public_id, "preview": True},
    ).json()

    # the survivor changes after the preview was issued
    survivor.name_canonical = "Renamed before applying"
    db.commit()

    resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={
            "absorb_public_id": absorbed.public_id,
            "preview": False,
            "preview_token": preview["data"]["preview_token"],
            "reason": "dup",
        },
    )
    assert resp.status_code == 409


# ==================================================================================== unmerge
def test_admin_unmerge_unknown_event_is_404(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="80")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{proposal.public_id}/unmerge",
        json={"merge_event_id": "evt_0000000000", "reason": "x"},
    )
    assert resp.status_code == 404


def test_admin_unmerge_wrong_event_type_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="81")
    other_event = Event(
        subject_type="proposal",
        subject_id=proposal.id,
        event_type="status_change",
        observed_at=dt.datetime.now(UTC),
        before={"lifecycle_state": "announced"},
        after={"lifecycle_state": "filed"},
        idempotency_key="not-a-merge-event-for-admin-test",
    )
    db.add(other_event)
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{proposal.public_id}/unmerge",
        json={"merge_event_id": public_id("evt", other_event.id), "reason": "x"},
    )
    assert resp.status_code == 409


def test_admin_unmerge_missing_reason_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="82")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{proposal.public_id}/unmerge", json={"merge_event_id": "evt_0000000000"}
    )
    assert resp.status_code == 400


# ==================================================================== resolution candidates
def test_admin_resolution_candidates_requires_admin(client, db):
    resp = client.get("/admin/v1/resolution-candidates")
    assert resp.status_code == 401


def test_admin_list_resolution_candidates_happy_path(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="90", suffix_b="91")
    make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)

    resp = client.get("/admin/v1/resolution-candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ResolutionCandidateListResponse", body)
    assert len(body["data"]) == 1
    assert body["data"][0]["status"] == "pending"
    assert body["data"][0]["decision"] is None


def test_admin_resolution_candidates_decided_filter_excludes_pending(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="92", suffix_b="93")
    make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)

    resp = client.get("/admin/v1/resolution-candidates?status=decided")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_admin_decide_resolution_candidate_confirm_merges(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="94", suffix_b="95")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)
    candidate_id = public_id("rc", decision.id)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{candidate_id}/decide",
        json={"decision": "same", "reason": "same project, two queue rows"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ResolutionCandidateDetailResponse", body)
    assert body["data"]["status"] == "decided"
    assert body["data"]["decision"] == "same"
    assert body["data"]["merge_event_id"]
    assert body["data"]["decided_by_user_id"]

    db.expire_all()
    left_id, right_id = sorted((left.id, right.id), key=str)
    left_row = db.get(Proposal, left_id)
    right_row = db.get(Proposal, right_id)
    assert right_row.merged_into_id == left_row.id
    decision_row = db.get(ResolutionDecision, decision.id)
    assert decision_row.status == "confirmed"
    assert decision_row.decided_by_user_id is not None


def test_admin_decide_resolution_candidate_reject_does_not_merge(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="96", suffix_b="97")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)
    candidate_id = public_id("rc", decision.id)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{candidate_id}/decide",
        json={"decision": "different", "reason": "two distinct, co-located projects"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ResolutionCandidateDetailResponse", body)
    assert body["data"]["decision"] == "different"
    assert body["data"]["merge_event_id"] is None

    db.expire_all()
    left_id, right_id = sorted((left.id, right.id), key=str)
    assert db.get(Proposal, left_id).merged_into_id is None
    assert db.get(Proposal, right_id).merged_into_id is None
    ev = db.query(Event).filter_by(event_type="admin_edit", subject_id=left_id).one()
    assert ev.after["status"] == "rejected"


def test_admin_decide_resolution_candidate_defer_leaves_it_pending(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="98", suffix_b="99")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)
    candidate_id = public_id("rc", decision.id)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{candidate_id}/decide",
        json={"decision": "defer", "reason": "need to check the FERC docket first"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "pending"

    db.expire_all()
    assert db.get(ResolutionDecision, decision.id).status == "proposed"


def test_admin_decide_resolution_candidate_already_decided_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="100", suffix_b="101")
    decision = make_resolution_decision(db, left, right, status="confirmed")
    db.commit()
    admin_login(client, db)
    candidate_id = public_id("rc", decision.id)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{candidate_id}/decide",
        json={"decision": "same", "reason": "x"},
    )
    assert resp.status_code == 409


def test_admin_decide_resolution_candidate_not_found(client, db):
    admin_login(client, db)
    resp = client.post(
        "/admin/v1/resolution-candidates/rc_0000000000/decide",
        json={"decision": "same", "reason": "x"},
    )
    assert resp.status_code == 404


# ============================================================================== extractions
def test_admin_list_extractions_requires_admin(client, db):
    resp = client.get("/admin/v1/extractions")
    assert resp.status_code == 401


def test_admin_list_extractions_happy_path(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="110")
    doc = make_document(db, source, licence)
    make_extraction(db, proposal, doc, source, licence)
    db.commit()
    admin_login(client, db)

    resp = client.get("/admin/v1/extractions")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ExtractionListResponse", body)
    assert len(body["data"]) == 1
    assert body["data"][0]["status"] == "proposed"
    assert body["data"][0]["cost_usd"] == 0.0


def test_admin_accept_extraction_changes_the_field_and_audits(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="111", lifecycle_state="filed")
    doc = make_document(db, source, licence)
    extraction = make_extraction(
        db, proposal, doc, source, licence, field_path="lifecycle_state", value="permitted"
    )
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/extractions/{extraction.public_id}/accept",
        json={"reason": "matches the filed permit order"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ExtractionDetailResponse", body)
    assert body["data"]["status"] == "accepted"
    assert body["data"]["applied_event_id"]

    db.expire_all()
    assert db.get(Proposal, proposal.id).lifecycle_state == "permitted"
    ev = db.query(Event).filter_by(event_type="extraction_accepted", subject_id=proposal.id).one()
    assert ev.before == {"lifecycle_state": "filed"}
    assert ev.after == {"lifecycle_state": "permitted"}
    assert db.get(Extraction, extraction.id).accepted_by_user_id is not None


def test_admin_reject_extraction_leaves_field_unchanged(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="112", lifecycle_state="filed")
    doc = make_document(db, source, licence)
    extraction = make_extraction(
        db, proposal, doc, source, licence, field_path="lifecycle_state", value="permitted"
    )
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/extractions/{extraction.public_id}/reject",
        json={"reason": "citation does not actually support this"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ExtractionDetailResponse", body)
    assert body["data"]["status"] == "rejected"

    db.expire_all()
    assert db.get(Proposal, proposal.id).lifecycle_state == "filed"


def test_admin_reject_extraction_missing_reason_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="113")
    doc = make_document(db, source, licence)
    extraction = make_extraction(db, proposal, doc, source, licence)
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/extractions/{extraction.public_id}/reject", json={})
    assert resp.status_code == 400


def test_admin_accept_extraction_already_decided_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="114")
    doc = make_document(db, source, licence)
    extraction = make_extraction(db, proposal, doc, source, licence, status="accepted")
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/extractions/{extraction.public_id}/accept", json={})
    assert resp.status_code == 409


def test_admin_accept_extraction_disallowed_field_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="115")
    doc = make_document(db, source, licence)
    extraction = make_extraction(db, proposal, doc, source, licence, field_path="source_count", value=5)
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/extractions/{extraction.public_id}/accept", json={})
    assert resp.status_code == 409


def test_admin_accept_extraction_gate_unmet_when_licence_withholds_derived(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    restricted_licence = make_restricted_derived_licence(db)
    proposal = make_visible_proposal(db, source, public_id_suffix="116")
    doc = make_document(db, source, licence)
    extraction = make_extraction(db, proposal, doc, source, restricted_licence)
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/extractions/{extraction.public_id}/accept", json={})
    assert resp.status_code == 422


def test_admin_accept_extraction_not_found(client, db):
    admin_login(client, db)
    resp = client.post("/admin/v1/extractions/ext_0000000000/accept", json={})
    assert resp.status_code == 404


def test_admin_reject_extraction_not_found(client, db):
    admin_login(client, db)
    resp = client.post("/admin/v1/extractions/ext_0000000000/reject", json={"reason": "x"})
    assert resp.status_code == 404


def test_admin_reject_extraction_already_decided_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="117")
    doc = make_document(db, source, licence)
    extraction = make_extraction(db, proposal, doc, source, licence, status="rejected")
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/extractions/{extraction.public_id}/reject", json={"reason": "x"})
    assert resp.status_code == 409


def test_admin_accept_extraction_payload_missing_field_path_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="118")
    doc = make_document(db, source, licence)
    extraction = make_extraction(db, proposal, doc, source, licence, field_path="technology", value="x")
    extraction.payload = {}  # the extraction proposes nothing usable
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/extractions/{extraction.public_id}/accept", json={})
    assert resp.status_code == 409


def test_admin_accept_extraction_coerces_a_date_field(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="119")
    doc = make_document(db, source, licence)
    extraction = make_extraction(
        db, proposal, doc, source, licence, field_path="proposed_online_date", value="2027-06-01"
    )
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/extractions/{extraction.public_id}/accept", json={"reason": "per filing"})
    assert resp.status_code == 200
    assert_valid(spec, "ExtractionDetailResponse", resp.json())

    db.expire_all()
    assert str(db.get(Proposal, proposal.id).proposed_online_date) == "2027-06-01"


def test_admin_accept_extraction_reports_model_call_cost(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="120")
    doc = make_document(db, source, licence)
    call = ModelCall(purpose="extract", alias="fast", source_id=source.id, cost_usd=0.0042)
    db.add(call)
    db.flush()
    extraction = make_extraction(db, proposal, doc, source, licence)
    extraction.model_call_id = call.id
    db.commit()
    admin_login(client, db)

    resp = client.get("/admin/v1/extractions")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["cost_usd"] == 0.0042


def test_admin_list_extractions_filters_by_subject_type_and_source_id(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="121")
    doc = make_document(db, source, licence)
    make_extraction(db, proposal, doc, source, licence)
    db.commit()
    admin_login(client, db)

    matching = client.get(f"/admin/v1/extractions?subject_type=proposal&source_id={source.id}")
    assert matching.status_code == 200
    assert len(matching.json()["data"]) == 1

    non_matching = client.get("/admin/v1/extractions?subject_type=opportunity")
    assert non_matching.status_code == 200
    assert non_matching.json()["data"] == []


def test_admin_list_extractions_bad_status_vocab_is_400(client, db):
    admin_login(client, db)
    resp = client.get("/admin/v1/extractions?status=not-a-status")
    assert resp.status_code == 400


# ============================================================== additional coverage: proposal
def test_admin_update_proposal_sets_proposed_online_date_and_sponsor(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="122")
    sponsor = make_org(db, name="New Sponsor LLC")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/proposals/{proposal.public_id}",
        json={
            "proposed_online_date": "2028-01-15",
            "sponsor_org_id": sponsor.public_id,
            "reason": "sponsor and target date confirmed by filing",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminProposalDetailResponse", body)
    assert body["data"]["proposed_online_date"] == "2028-01-15"
    assert body["data"]["sponsor"]["public_id"] == sponsor.public_id

    db.expire_all()
    ev = db.query(Event).filter_by(event_type="admin_edit", subject_id=proposal.id).one()
    assert ev.after["sponsor_org_id"] == sponsor.public_id
    assert ev.after["proposed_online_date"] == "2028-01-15"


# ============================================================ additional coverage: opportunity
def test_admin_update_opportunity_not_found(client, db):
    admin_login(client, db)
    resp = client.patch("/admin/v1/opportunities/opp_0000000000", json={"reason": "x", "status": "closed"})
    assert resp.status_code == 404


def test_admin_update_opportunity_unknown_issuer_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="5")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/opportunities/{opportunity.public_id}",
        json={"issuer_org_id": "org_0000000000", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_update_opportunity_sets_dates_and_issuer_and_clears_override(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="6")
    issuer = make_org(db, name="Issuing Utility")
    db.commit()
    admin_login(client, db)
    url = f"/admin/v1/opportunities/{opportunity.public_id}"

    first = client.patch(
        url,
        json={
            "open_at": "2026-10-01",
            "due_at": "2026-12-01T00:00:00Z",
            "issuer_org_id": issuer.public_id,
            "reason": "issuer and dates confirmed",
        },
    )
    assert first.status_code == 200
    body = first.json()
    assert_valid(spec, "AdminOpportunityDetailResponse", body)
    assert body["data"]["open_at"] == "2026-10-01"
    assert body["data"]["issuer"]["public_id"] == issuer.public_id
    assert "issuer_org_id" in body["data"]["overrides"]

    second = client.patch(
        url, json={"status": "closed", "reason": "closed", "clear_overrides": ["issuer_org_id"]}
    )
    assert second.status_code == 200
    assert "issuer_org_id" not in second.json()["data"]["overrides"]


def test_admin_update_opportunity_no_change_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="7", status="open")
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/opportunities/{opportunity.public_id}", json={"status": "open", "reason": "no-op"}
    )
    assert resp.status_code == 400


# ============================================================ additional coverage: organization
def test_admin_update_organization_missing_reason_is_400(client, db):
    org = make_org(db)
    db.commit()
    admin_login(client, db)

    resp = client.patch(f"/admin/v1/organizations/{org.public_id}", json={"is_curated_issuer": True})
    assert resp.status_code == 400


def test_admin_update_organization_no_change_is_400(client, db):
    org = make_org(db)
    db.commit()
    admin_login(client, db)

    resp = client.patch(
        f"/admin/v1/organizations/{org.public_id}",
        json={"name_canonical": org.name_canonical, "reason": "no-op"},
    )
    assert resp.status_code == 400


# ============================================================= additional coverage: publish-state
def test_admin_set_publish_state_invalid_record_type_is_400(client, db):
    admin_login(client, db)
    resp = client.put(
        "/admin/v1/records/widgets/prop_0000000000/publish-state",
        json={"publish_state": "public", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_set_publish_state_for_opportunity_going_public(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    opportunity = make_visible_opportunity(db, source, public_id_suffix="8", status="open")
    opportunity.publish_state = "pending_review"
    opportunity.published_at = None
    opportunity.public_at = None
    db.commit()
    admin_login(client, db)

    resp = client.put(
        f"/admin/v1/records/opportunities/{opportunity.public_id}/publish-state",
        json={"publish_state": "public", "reason": "reviewed and cleared for publication"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "RecordPublishStateResponse", body)

    db.expire_all()
    opportunity_row = db.get(Opportunity, opportunity.id)
    assert opportunity_row.publish_state == "public"
    assert opportunity_row.published_at is not None
    assert opportunity_row.public_at is not None
    ev = db.query(Event).filter_by(event_type="published", subject_id=opportunity.id).one()
    assert ev.reason == "reviewed and cleared for publication"


# ===================================================================== additional coverage: merge
def test_admin_merge_survivor_not_found(client, db):
    admin_login(client, db)
    resp = client.post(
        "/admin/v1/proposals/prop_0000000000/merge",
        json={"absorb_public_id": "prop_0000000001", "preview": True},
    )
    assert resp.status_code == 404


def test_admin_merge_absorbed_not_found(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="123")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{proposal.public_id}/merge",
        json={"absorb_public_id": "prop_0000000001", "preview": True},
    )
    assert resp.status_code == 404


def test_admin_merge_missing_absorb_public_id_or_preview_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="124")
    db.commit()
    admin_login(client, db)

    resp = client.post(f"/admin/v1/proposals/{proposal.public_id}/merge", json={"preview": True})
    assert resp.status_code == 400


def test_admin_merge_bad_field_choice_value_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    survivor, absorbed = _make_proposal_pair(db, source, suffix_a="125", suffix_b="126")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={
            "absorb_public_id": absorbed.public_id,
            "preview": True,
            "field_choices": {"name_canonical": "not-a-side"},
        },
    )
    assert resp.status_code == 400


def test_admin_merge_apply_missing_reason_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    survivor, absorbed = _make_proposal_pair(db, source, suffix_a="127", suffix_b="128")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={"absorb_public_id": absorbed.public_id, "preview": False, "preview_token": "whatever"},
    )
    assert resp.status_code == 400


def test_admin_merge_apply_with_garbage_preview_token_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    survivor, absorbed = _make_proposal_pair(db, source, suffix_a="129", suffix_b="130")
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={
            "absorb_public_id": absorbed.public_id,
            "preview": False,
            "preview_token": "not-a-real-token",
            "reason": "x",
        },
    )
    assert resp.status_code == 400


def test_admin_merge_field_choices_absorbed_side_applied(client, db, spec):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    survivor, absorbed = _make_proposal_pair(db, source, suffix_a="131", suffix_b="132")
    db.commit()
    admin_login(client, db)

    preview = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={
            "absorb_public_id": absorbed.public_id,
            "preview": True,
            "field_choices": {"name_canonical": "absorbed"},
        },
    ).json()

    apply_resp = client.post(
        f"/admin/v1/proposals/{survivor.public_id}/merge",
        json={
            "absorb_public_id": absorbed.public_id,
            "preview": False,
            "preview_token": preview["data"]["preview_token"],
            "field_choices": {"name_canonical": "absorbed"},
            "reason": "keeping the absorbed record's name",
        },
    )
    assert apply_resp.status_code == 200
    assert_valid(spec, "MergeResponse", apply_resp.json())

    db.expire_all()
    assert db.get(Proposal, survivor.id).name_canonical == absorbed.name_canonical


# =================================================================== additional coverage: unmerge
def test_admin_unmerge_proposal_not_found(client, db):
    admin_login(client, db)
    resp = client.post(
        "/admin/v1/proposals/prop_0000000000/unmerge",
        json={"merge_event_id": "evt_0000000000", "reason": "x"},
    )
    assert resp.status_code == 404


# ========================================================= additional coverage: resolution queue
def test_admin_list_resolution_candidates_subject_type_excludes_proposal(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="133", suffix_b="134")
    make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)

    resp = client.get("/admin/v1/resolution-candidates?subject_type=opportunity")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_admin_list_resolution_candidates_bad_status_is_400(client, db):
    admin_login(client, db)
    resp = client.get("/admin/v1/resolution-candidates?status=not-a-status")
    assert resp.status_code == 400


def test_admin_decide_resolution_candidate_bad_decision_vocab_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="135", suffix_b="136")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{public_id('rc', decision.id)}/decide",
        json={"decision": "not-a-decision", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_decide_resolution_candidate_missing_reason_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="137", suffix_b="138")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{public_id('rc', decision.id)}/decide",
        json={"decision": "same"},
    )
    assert resp.status_code == 400


def test_admin_decide_resolution_candidate_confirm_right_side_survives(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="139", suffix_b="140")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)
    left_id, right_id = sorted((left.id, right.id), key=str)
    right_row = db.get(Proposal, right_id)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{public_id('rc', decision.id)}/decide",
        json={
            "decision": "same",
            "surviving_public_id": right_row.public_id,
            "reason": "right side is more complete",
        },
    )
    assert resp.status_code == 200

    db.expire_all()
    assert db.get(Proposal, left_id).merged_into_id == right_id


def test_admin_decide_resolution_candidate_invalid_surviving_public_id_is_400(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="141", suffix_b="142")
    other = make_visible_proposal(db, source, public_id_suffix="143")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)

    resp = client.post(
        f"/admin/v1/resolution-candidates/{public_id('rc', decision.id)}/decide",
        json={"decision": "same", "surviving_public_id": other.public_id, "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_decide_resolution_candidate_confirm_when_already_merged_is_409(client, db):
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    left, right = _make_proposal_pair(db, source, suffix_a="144", suffix_b="145")
    third = make_visible_proposal(db, source, public_id_suffix="146")
    decision = make_resolution_decision(db, left, right)
    db.commit()
    admin_login(client, db)

    # merge `left` into `third` first, so `left` is already absorbed elsewhere
    preview = client.post(
        f"/admin/v1/proposals/{third.public_id}/merge",
        json={"absorb_public_id": left.public_id, "preview": True},
    ).json()
    client.post(
        f"/admin/v1/proposals/{third.public_id}/merge",
        json={
            "absorb_public_id": left.public_id,
            "preview": False,
            "preview_token": preview["data"]["preview_token"],
            "reason": "prior unrelated merge",
        },
    )

    resp = client.post(
        f"/admin/v1/resolution-candidates/{public_id('rc', decision.id)}/decide",
        json={"decision": "same", "reason": "x"},
    )
    assert resp.status_code == 409
