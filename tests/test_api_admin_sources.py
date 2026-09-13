"""Tests for `services/api/admin_sources.py` (source health, the licence gate, cost reporting and
the audit log). Mounts the module's router onto the shared app directly (the coordinator's job
once this module is wired in `services/api/app.py`, `services/api/admin_sources.py`'s module
docstring) so this file can exercise it standalone.

Reuses `tests/conftest.py`'s `client`/`db`/`login`/`make_account`/`make_user` fixtures and
`services/api/conftest.py`'s `make_open_licence`/`make_attribution_licence`/`make_public_source`
factories per the task brief, plus `tests/test_api_contract.py`'s `assert_valid`/`spec` pattern.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import yaml

from services.api.admin_sources import get_source_runner
from services.api.admin_sources import router as admin_sources_router
from services.api.app import app
from services.api.common import utcnow
from services.api.conftest import (
    make_attribution_licence,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Event, Licence, Match, ModelCall, Source, SourceRun
from services.ids import public_id
from tests.conftest import login, make_account, make_user
from tests.test_api_contract import assert_valid

UTC = dt.UTC
#: Same file `tests/test_api_contract.py`'s `spec` fixture reads; defined locally (rather than
# imported) so a `spec` test-function parameter doesn't ruff-F811 against an imported name of the
# same identifier — `assert_valid` (the substantive shared piece) is still reused via import.
_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


app.include_router(admin_sources_router)


class _FakeSourceRunner:
    """Test double for `services.api.admin_sources.SourceRunner`: creates the `source_run` row
    without touching Procrastinate/Postgres (module docstring: "never run a connector inline")."""

    def enqueue(self, db, source, *, trigger, requested_by):
        run = SourceRun(
            source_id=source.id,
            trigger=trigger,
            started_at=utcnow(),
            status="running",
            egress_class=source.egress,
        )
        db.add(run)
        db.flush()
        return run


@pytest.fixture(autouse=True)
def _fake_runner():
    app.dependency_overrides[get_source_runner] = lambda: _FakeSourceRunner()
    yield
    app.dependency_overrides.pop(get_source_runner, None)


# --------------------------------------------------------------------------------------- helpers
def _operator(db):
    account = make_account(db, entitlement="admin", name="Ops")
    return make_user(db, account, email=f"op{id(account)}@example.com", role="operator")


def _legal(db):
    account = make_account(db, entitlement="admin", name="Legal")
    return make_user(db, account, email=f"legal{id(account)}@example.com", role="legal")


def _viewer(db):
    account = make_account(db, entitlement="public", name="Viewer")
    return make_user(db, account, email=f"viewer{id(account)}@example.com", role="viewer")


def _make_source(db, licence: Licence, *, source_id: str = "us.test.admin_source", **overrides):
    defaults = {
        "id": source_id,
        "name": "Test Admin Source",
        "category": "generation_queue",
        "jurisdiction": "US-TX",
        "operator": "Test Operator",
        "url": "https://example.org/queue",
        "access": "bulk_file",
        "cadence": "weekly",
        "licence_id": licence.id,
        "publish_state": "ingest_only",
        "host": "example.org",
        "egress": "plain",
    }
    defaults.update(overrides)
    src = Source(**defaults)
    db.add(src)
    db.flush()
    return src


def _restricted_licence(db, id_="restricted-lic"):
    lic = Licence(
        id=id_,
        name="Restricted Licence",
        reuse_class="restricted",
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def _no_evidence_licence(db, id_="no-evidence-lic"):
    lic = Licence(id=id_, name="No Evidence Licence", reuse_class="open", gate_flag=False)
    db.add(lic)
    db.flush()
    return lic


def _gated_licence(db, id_="gated-lic"):
    lic = Licence(
        id=id_,
        name="Gated Licence",
        reuse_class="open",
        gate_flag=True,
        gate_name="G-TEST",
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


# ============================================================================ GET /admin/v1/sources
def test_list_sources_requires_auth(client, db):
    resp = client.get("/admin/v1/sources")
    assert resp.status_code == 401


def test_list_sources_requires_operator_role(client, db):
    viewer = _viewer(db)
    db.commit()
    login(client, db, viewer)
    resp = client.get("/admin/v1/sources")
    assert resp.status_code == 403


def test_list_sources_happy_path(client, db, spec):
    lic = make_open_licence(db)
    _make_source(db, lic)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminSourceListResponse", body)
    assert any(s["source_id"] == "us.test.admin_source" for s in body["data"])


def test_list_sources_filters_by_health(client, db):
    lic = make_open_licence(db)
    _make_source(db, lic, source_id="us.test.ok", health="ok")
    _make_source(db, lic, source_id="us.test.failing", health="failing")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/sources", params={"health": "failing"})
    assert resp.status_code == 200
    ids = [s["source_id"] for s in resp.json()["data"]]
    assert ids == ["us.test.failing"]


# ============================================================================ GET /sources/{id}
def test_get_source_not_found(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/sources/does.not.exist")
    assert resp.status_code == 404


def test_get_source_happy_path_includes_last_run(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    run = SourceRun(
        source_id=src.id, trigger="schedule", started_at=utcnow(), status="ok", egress_class="plain"
    )
    db.add(run)
    db.flush()
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get(f"/admin/v1/sources/{src.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminSourceDetailResponse", body)
    assert body["data"]["last_run"]["status"] == "ok"


# ============================================================================ POST /sources
def test_create_source_happy_path(client, db, spec):
    lic = make_attribution_licence(db)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "us.test.curated_issuer",
            "name": "Curated Issuer",
            "operator": "Some Utility",
            "url": "https://issuer.example.org/rfps",
            "access": "html",
            "cadence": "weekly",
            "licence_id": lic.id,
            "reason": "US-303 curated issuer onboarding",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert_valid(spec, "AdminSourceDetailResponse", body)
    assert body["data"]["implemented"] is False
    assert body["data"]["publish_state"] == "ingest_only"
    assert body["data"]["host"] == "issuer.example.org"

    events = db.query(Event).filter_by(event_type="created").all()
    assert len(events) == 1
    assert events[0].reason == "US-303 curated issuer onboarding"


def test_create_source_requires_reason(client, db):
    lic = make_attribution_licence(db)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "us.test.curated_issuer2",
            "name": "X",
            "operator": "Y",
            "url": "https://example.org",
            "access": "html",
            "cadence": "weekly",
            "licence_id": lic.id,
        },
    )
    assert resp.status_code == 400


def test_create_source_duplicate_id_is_409(client, db):
    lic = make_attribution_licence(db)
    _make_source(db, lic, source_id="us.test.dupe")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "us.test.dupe",
            "name": "X",
            "operator": "Y",
            "url": "https://example.org",
            "access": "html",
            "cadence": "weekly",
            "licence_id": lic.id,
            "reason": "dup test",
        },
    )
    assert resp.status_code == 409


def test_create_source_unknown_licence_is_404(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "us.test.newissuer",
            "name": "X",
            "operator": "Y",
            "url": "https://example.org",
            "access": "html",
            "cadence": "weekly",
            "licence_id": "does-not-exist",
            "reason": "test",
        },
    )
    assert resp.status_code == 404


# ============================================================================ PATCH /sources/{id}
def test_update_source_happy_path_and_audit(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.patch(
        f"/admin/v1/sources/{src.id}",
        json={"paused": True, "reason": "pausing for maintenance"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminSourceDetailResponse", body)
    assert body["data"]["paused"] is True

    events = db.query(Event).filter_by(event_type="admin_edit").all()
    assert len(events) == 1
    assert events[0].reason == "pausing for maintenance"
    assert events[0].before["paused"] is False
    assert events[0].after["paused"] is True


def test_update_source_requires_reason(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.patch(f"/admin/v1/sources/{src.id}", json={"paused": True})
    assert resp.status_code == 400


def test_update_source_requires_a_field_besides_reason(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.patch(f"/admin/v1/sources/{src.id}", json={"reason": "nothing to change"})
    assert resp.status_code == 400


def test_update_source_not_found(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.patch("/admin/v1/sources/does.not.exist", json={"paused": True, "reason": "x"})
    assert resp.status_code == 404


# ============================================================================ POST /sources/{id}/run
def test_run_source_happy_path(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic, implemented=True)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.post(f"/admin/v1/sources/{src.id}/run", json={"reason": "manual run for testing"})
    assert resp.status_code == 202
    body = resp.json()
    assert_valid(spec, "SourceRunDetailResponse", body)
    assert body["data"]["trigger"] == "manual"
    assert body["data"]["status"] == "running"

    run_row = db.query(SourceRun).filter_by(source_id=src.id).one()
    assert run_row.status == "running"


def test_run_source_requires_reason(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic, implemented=True)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/sources/{src.id}/run", json={})
    assert resp.status_code == 400


def test_run_source_not_found(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post("/admin/v1/sources/does.not.exist/run", json={"reason": "x"})
    assert resp.status_code == 404


def test_run_source_while_paused_is_409(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic, implemented=True, paused=True)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/sources/{src.id}/run", json={"reason": "x"})
    assert resp.status_code == 409


def test_run_source_unimplemented_is_409(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic, implemented=False)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/sources/{src.id}/run", json={"reason": "x"})
    assert resp.status_code == 409


def test_run_source_already_running_is_409(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic, implemented=True)
    db.add(SourceRun(source_id=src.id, trigger="schedule", started_at=utcnow(), status="running"))
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/sources/{src.id}/run", json={"reason": "x"})
    assert resp.status_code == 409


# ============================================================== PUT /sources/{id}/publish-state
def test_publish_state_to_public_happy_path(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.put(
        f"/admin/v1/sources/{src.id}/publish-state",
        json={"publish_state": "public", "reason": "licence cleared"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminSourceDetailResponse", body)
    assert body["data"]["publish_state"] == "public"

    events = db.query(Event).filter_by(event_type="published").all()
    assert len(events) == 1
    assert events[0].before["publish_state"] == "ingest_only"
    assert events[0].after["publish_state"] == "public"


def test_publish_state_ingest_only_is_always_allowed(client, db):
    lic = _restricted_licence(db)
    src = _make_source(db, lic, source_id="us.test.restricted")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.put(
        f"/admin/v1/sources/{src.id}/publish-state",
        json={"publish_state": "ingest_only", "reason": "keep ingest-only"},
    )
    assert resp.status_code == 200


def test_publish_state_gate_unmet_restricted_reuse(client, db):
    lic = _restricted_licence(db)
    src = _make_source(db, lic, source_id="us.test.restricted2")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.put(
        f"/admin/v1/sources/{src.id}/publish-state",
        json={"publish_state": "public", "reason": "try anyway"},
    )
    assert resp.status_code == 422
    assert "reuse_class" in resp.json()["detail"]


def test_publish_state_gate_unmet_missing_evidence(client, db):
    lic = _no_evidence_licence(db)
    src = _make_source(db, lic, source_id="us.test.noevidence")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.put(
        f"/admin/v1/sources/{src.id}/publish-state",
        json={"publish_state": "public", "reason": "try anyway"},
    )
    assert resp.status_code == 422
    assert "evidence" in resp.json()["detail"]


def test_publish_state_gate_unmet_gate_flag_set(client, db):
    lic = _gated_licence(db)
    src = _make_source(db, lic, source_id="us.test.gated")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.put(
        f"/admin/v1/sources/{src.id}/publish-state",
        json={"publish_state": "public", "reason": "try anyway"},
    )
    assert resp.status_code == 422
    assert "gate_flag" in resp.json()["detail"]


def test_publish_state_not_found(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.put(
        "/admin/v1/sources/does.not.exist/publish-state",
        json={"publish_state": "public", "reason": "x"},
    )
    assert resp.status_code == 404


def test_publish_state_requires_reason(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.put(f"/admin/v1/sources/{src.id}/publish-state", json={"publish_state": "public"})
    assert resp.status_code == 400


# =============================================================================== GET /source-runs
def test_list_source_runs_happy_path_and_filter(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    db.add(SourceRun(source_id=src.id, trigger="schedule", started_at=utcnow(), status="ok"))
    db.add(SourceRun(source_id=src.id, trigger="schedule", started_at=utcnow(), status="failed"))
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/source-runs")
    assert resp.status_code == 200
    assert_valid(spec, "SourceRunListResponse", resp.json())

    resp2 = client.get("/admin/v1/source-runs", params={"status": "failed"})
    assert resp2.status_code == 200
    assert all(r["status"] == "failed" for r in resp2.json()["data"])


def test_get_source_run_happy_path(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    run = SourceRun(source_id=src.id, trigger="manual", started_at=utcnow(), status="ok")
    db.add(run)
    db.flush()
    run_public_id = public_id("run", run.id)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get(f"/admin/v1/source-runs/{run_public_id}")
    assert resp.status_code == 200
    assert_valid(spec, "SourceRunDetailResponse", resp.json())


def test_get_source_run_not_found(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/source-runs/run_0000000000")
    assert resp.status_code == 404


# =============================================================================== GET /snapshots
def test_get_snapshot_happy_path(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    run = SourceRun(source_id=src.id, trigger="schedule", started_at=utcnow(), status="ok")
    db.add(run)
    db.flush()
    from services.db.models import Snapshot

    snap = Snapshot(
        source_id=src.id,
        source_run_id=run.id,
        object_key=f"raw/{src.id}/2026/09/12/abc.xlsx",
        sha256="0" * 64,
        byte_size=1000,
        content_type="application/octet-stream",
        fetched_url=src.url,
        http_status=200,
        retrieved_at=utcnow(),
        licence_id=lic.id,
    )
    db.add(snap)
    db.flush()
    snap_public_id = public_id("snap", snap.id)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get(f"/admin/v1/snapshots/{snap_public_id}")
    assert resp.status_code == 200
    assert_valid(spec, "SnapshotDetailResponse", resp.json())


def test_get_snapshot_not_found(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/snapshots/snap_0000000000")
    assert resp.status_code == 404


# ======================================================================= PUT /licences/{id}/gate
def test_set_licence_gate_requires_legal_role(client, db):
    lic = _gated_licence(db, id_="gate-role-test")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.put(
        f"/admin/v1/licences/{lic.id}/gate",
        json={"gate_flag": False, "reason": "clearing", "classified_by": "legal-compliance"},
    )
    assert resp.status_code == 403


def test_set_licence_gate_clear_happy_path(client, db, spec):
    lic = _gated_licence(db, id_="gate-clear-test")
    legal = _legal(db)
    db.commit()
    login(client, db, legal)

    resp = client.put(
        f"/admin/v1/licences/{lic.id}/gate",
        json={"gate_flag": False, "reason": "counsel confirmed terms"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminLicenceDetailResponse", body)
    assert body["data"]["gate_flag"] is False

    events = db.query(Event).filter_by(event_type="gate_cleared").all()
    assert len(events) == 1
    assert events[0].reason == "counsel confirmed terms"


def test_set_licence_gate_clear_missing_evidence_is_gate_unmet(client, db):
    lic = Licence(id="gate-no-evidence", name="No evidence", reuse_class="open", gate_flag=True)
    db.add(lic)
    db.flush()
    legal = _legal(db)
    db.commit()
    login(client, db, legal)
    resp = client.put(f"/admin/v1/licences/{lic.id}/gate", json={"gate_flag": False, "reason": "clearing"})
    assert resp.status_code == 422


def test_set_licence_gate_requires_reason(client, db):
    lic = _gated_licence(db, id_="gate-reason-test")
    legal = _legal(db)
    db.commit()
    login(client, db, legal)
    resp = client.put(f"/admin/v1/licences/{lic.id}/gate", json={"gate_flag": False})
    assert resp.status_code == 400


def test_set_licence_gate_not_found(client, db):
    legal = _legal(db)
    db.commit()
    login(client, db, legal)
    resp = client.put("/admin/v1/licences/does-not-exist/gate", json={"gate_flag": False, "reason": "x"})
    assert resp.status_code == 404


def test_set_licence_gate_reclassification_creates_new_licence_and_repoints_sources(client, db, spec):
    lic = _gated_licence(db, id_="reclass-test")
    src = _make_source(db, lic, source_id="us.test.reclass")
    legal = _legal(db)
    db.commit()
    login(client, db, legal)

    resp = client.put(
        f"/admin/v1/licences/{lic.id}/gate",
        json={
            "gate_flag": False,
            "reuse_class": "attribution",
            "reason": "reclassified after review",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminLicenceDetailResponse", body)
    new_licence_id = body["data"]["licence_id"]
    assert new_licence_id != lic.id
    assert body["data"]["reuse_class"] == "attribution"

    db.refresh(src)
    assert src.licence_id == new_licence_id

    events = db.query(Event).filter_by(event_type="licence_reclassified").all()
    assert len(events) == 1
    assert events[0].before["reuse_class"] == "open"
    assert events[0].after["reuse_class"] == "attribution"


# =================================================================================== GET /costs
def test_get_costs_aggregates_model_calls_and_source_runs(client, db, spec):
    lic = make_open_licence(db)
    src = _make_source(db, lic, source_id="us.test.costsource")
    day = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    db.add(
        ModelCall(
            created_at=day,
            purpose="extract",
            source_id=src.id,
            alias="fast",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.02,
            cache_hit=True,
        )
    )
    db.add(
        ModelCall(
            created_at=day,
            purpose="extract",
            source_id=src.id,
            alias="fast",
            input_tokens=200,
            output_tokens=80,
            cost_usd=0.03,
            cache_hit=False,
        )
    )
    db.add(
        SourceRun(
            source_id=src.id,
            trigger="schedule",
            started_at=day,
            status="ok",
            cost_usd=0.10,
            model_calls=1,
            rows_changed=4,
        )
    )
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/costs", params={"day[from]": "2026-09-10", "day[to]": "2026-09-10"})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "CostReportResponse", body)
    data = body["data"]
    totals = data["totals"]
    assert round(totals["cost_usd"], 2) == round(0.02 + 0.03 + 0.10, 2)
    assert totals["model_calls"] == 3
    assert totals["input_tokens"] == 300
    assert totals["output_tokens"] == 130


def test_get_costs_empty_window_returns_empty_rows(client, db, spec):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/costs", params={"day[from]": "2020-01-01", "day[to]": "2020-01-02"})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "CostReportResponse", body)
    assert body["data"]["rows"] == []


def test_get_costs_requires_operator(client, db):
    resp = client.get("/admin/v1/costs")
    assert resp.status_code == 401


# =================================================================================== GET /audit
def test_list_audit_requires_auth(client, db):
    resp = client.get("/admin/v1/audit")
    assert resp.status_code == 401


def test_list_audit_happy_path_schema_valid_for_match_subject(client, db, spec):
    from services.api.audit import record_audit_event

    prop_lic = make_open_licence(db, id_="audit-prop-lic")
    src = make_public_source(db, prop_lic, id_="us.test.audit_src")
    proposal = make_visible_proposal(db, src, public_id_suffix="9")
    opportunity = make_visible_opportunity(db, src, public_id_suffix="9")
    match = Match(
        proposal_id=proposal.id,
        opportunity_id=opportunity.id,
        score=0.9,
        rationale={"rules_passed": ["technology"], "rules_failed": []},
        rationale_text="storage, TX",
        rule_set_version="match-rules@v1",
        created_by="rule",
    )
    db.add(match)
    db.flush()
    operator = _operator(db)
    record_audit_event(
        db,
        subject_type="match",
        subject_id=match.id,
        event_type="admin_edit",
        actor=operator,
        reason="test match edit",
        before={"status": "active"},
        after={"status": "removed"},
    )
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/audit", params={"subject_type": "match"})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "EventListResponse", body)
    assert body["data"][0]["reason"] == "test match edit"


def test_list_audit_includes_source_subject_rows_with_reason_and_diff(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic, source_id="us.test.auditsource")
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    patch_resp = client.patch(
        f"/admin/v1/sources/{src.id}", json={"paused": True, "reason": "audit trail check"}
    )
    assert patch_resp.status_code == 200

    resp = client.get("/admin/v1/audit", params={"subject_type": "source"})
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert any(r["reason"] == "audit trail check" and r["after"]["paused"] is True for r in rows)


def test_list_audit_filters_by_event_type_and_paginates(client, db):
    from services.api.audit import record_audit_event

    operator = _operator(db)
    for i in range(3):
        record_audit_event(
            db,
            subject_type="account",
            subject_id=operator.account_id,
            event_type="admin_edit",
            actor=operator,
            reason=f"edit {i}",
            before={"n": i},
            after={"n": i + 1},
        )
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/audit", params={"event_type": "admin_edit", "limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["page"]["has_more"] is True

    resp2 = client.get(
        "/admin/v1/audit",
        params={"event_type": "admin_edit", "limit": 2, "cursor": body["page"]["next_cursor"]},
    )
    assert resp2.status_code == 200
    assert len(resp2.json()["data"]) == 1


# ------------------------------------------------------------------ extra coverage: list filters
def test_list_sources_filters_category_publish_state_implemented_reuse_class(client, db):
    open_lic = make_open_licence(db, id_="filter-open")
    attr_lic = make_attribution_licence(db, id_="filter-attr")
    _make_source(
        db, open_lic, source_id="us.test.f1", category="permit", publish_state="public", implemented=True
    )
    _make_source(db, attr_lic, source_id="us.test.f2", category="funding", implemented=False)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    r1 = client.get("/admin/v1/sources", params={"category": "permit"})
    assert [s["source_id"] for s in r1.json()["data"]] == ["us.test.f1"]

    r2 = client.get("/admin/v1/sources", params={"publish_state": "public"})
    assert [s["source_id"] for s in r2.json()["data"]] == ["us.test.f1"]

    r3 = client.get("/admin/v1/sources", params={"implemented": "false"})
    assert [s["source_id"] for s in r3.json()["data"]] == ["us.test.f2"]

    r4 = client.get("/admin/v1/sources", params={"reuse_class": "attribution"})
    assert [s["source_id"] for s in r4.json()["data"]] == ["us.test.f2"]


# ------------------------------------------------------------------ extra coverage: create source
def test_create_source_rejects_bad_source_id_pattern(client, db):
    lic = make_attribution_licence(db)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "Not A Valid Id!!",
            "name": "X",
            "operator": "Y",
            "url": "https://example.org",
            "access": "html",
            "cadence": "weekly",
            "licence_id": lic.id,
            "reason": "test",
        },
    )
    assert resp.status_code == 400


def test_create_source_rejects_bad_access(client, db):
    lic = make_attribution_licence(db)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "us.test.badaccess",
            "name": "X",
            "operator": "Y",
            "url": "https://example.org",
            "access": "bulk_file",
            "cadence": "weekly",
            "licence_id": lic.id,
            "reason": "test",
        },
    )
    assert resp.status_code == 400


def test_create_source_with_issuer_org_marks_curated(client, db):
    from services.db.models import Organization
    from services.ids import public_id as _pid
    from services.ids import slugify

    lic = make_attribution_licence(db)
    org = Organization(
        public_id="",
        slug="",
        name_canonical="Issuer Co",
        name_normalised="issuer co",
        type="utility",
        country="US",
    )
    db.add(org)
    db.flush()
    org.public_id = _pid("org", org.id)
    org.slug = slugify(org.name_canonical)
    db.flush()
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "us.test.issuerorg",
            "name": "X",
            "operator": "Y",
            "url": "https://example.org",
            "access": "html",
            "cadence": "weekly",
            "licence_id": lic.id,
            "issuer_org_id": org.public_id,
            "reason": "test",
        },
    )
    assert resp.status_code == 201
    db.refresh(org)
    assert org.is_curated_issuer is True


def test_create_source_unknown_issuer_org_is_400(client, db):
    lic = make_attribution_licence(db)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(
        "/admin/v1/sources",
        json={
            "source_id": "us.test.badissuer",
            "name": "X",
            "operator": "Y",
            "url": "https://example.org",
            "access": "html",
            "cadence": "weekly",
            "licence_id": lic.id,
            "issuer_org_id": "org_doesnotexist000",
            "reason": "test",
        },
    )
    assert resp.status_code == 400


# ------------------------------------------------------------------ extra coverage: update source
def test_update_source_rejects_bad_lag_days(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.patch(f"/admin/v1/sources/{src.id}", json={"lag_days": 99, "reason": "bad lag"})
    assert resp.status_code == 400


# ------------------------------------------------------------------ extra coverage: run source
def test_run_source_rejects_bad_trigger(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic, implemented=True)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.post(f"/admin/v1/sources/{src.id}/run", json={"reason": "x", "trigger": "not-a-trigger"})
    assert resp.status_code == 400


def test_queued_source_runner_wraps_failures_as_unavailable(db):
    from services.api.admin_sources import QueuedSourceRunner

    lic = make_open_licence(db)
    src = _make_source(db, lic, source_id="us.test.queuedrunner", implemented=True)
    operator = _operator(db)
    db.commit()

    from services.api.errors import ProblemError

    with pytest.raises(ProblemError) as excinfo:
        QueuedSourceRunner().enqueue(db, src, trigger="manual", requested_by=operator)
    assert excinfo.value.code == "unavailable"


# ------------------------------------------------------------------ extra coverage: source-runs
def test_list_source_runs_filters_by_source_id_and_started_at(client, db):
    lic = make_open_licence(db)
    src1 = _make_source(db, lic, source_id="us.test.runs1")
    src2 = _make_source(db, lic, source_id="us.test.runs2")
    old = dt.datetime(2020, 1, 1, tzinfo=UTC)
    new = utcnow()
    db.add(SourceRun(source_id=src1.id, trigger="schedule", started_at=old, status="ok"))
    db.add(SourceRun(source_id=src2.id, trigger="schedule", started_at=new, status="ok"))
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/source-runs", params={"source_id": src1.id})
    assert [r["source_id"] for r in resp.json()["data"]] == [src1.id]

    resp2 = client.get("/admin/v1/source-runs", params={"started_at[from]": "2025-01-01T00:00:00Z"})
    assert all(r["source_id"] == src2.id for r in resp2.json()["data"])

    resp3 = client.get("/admin/v1/source-runs", params={"started_at[to]": "2020-06-01T00:00:00Z"})
    assert all(r["source_id"] == src1.id for r in resp3.json()["data"])


# ------------------------------------------------------------------ extra coverage: licence gate
def test_set_licence_gate_set_true_updates_optional_fields(client, db, spec):
    lic = _no_evidence_licence(db, id_="gate-set-true")
    legal = _legal(db)
    db.commit()
    login(client, db, legal)

    resp = client.put(
        f"/admin/v1/licences/{lic.id}/gate",
        json={
            "gate_flag": True,
            "gate_name": "G-NEW",
            "evidence_url": "https://example.org/new-terms",
            "evidence_retrieved_at": "2026-09-12T00:00:00Z",
            "evidence_object_key": "raw/licences/new.pdf",
            "classified_by": "legal-compliance",
            "contract_ref": "CR-123",
            "notes": "under review",
            "reason": "opening the gate pending counsel review",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AdminLicenceDetailResponse", body)
    assert body["data"]["gate_flag"] is True
    assert body["data"]["gate_name"] == "G-NEW"
    assert body["data"]["contract_ref"] == "CR-123"
    assert body["data"]["notes"] == "under review"

    events = db.query(Event).filter_by(event_type="admin_edit").all()
    assert any(e.subject_type == "licence" for e in events)


def test_set_licence_gate_rejects_bad_gate_flag_type(client, db):
    lic = _gated_licence(db, id_="gate-bad-flag")
    legal = _legal(db)
    db.commit()
    login(client, db, legal)
    resp = client.put(f"/admin/v1/licences/{lic.id}/gate", json={"gate_flag": "yes", "reason": "x"})
    assert resp.status_code == 400


def test_set_licence_gate_rejects_bad_reuse_class(client, db):
    lic = _gated_licence(db, id_="gate-bad-reuse")
    legal = _legal(db)
    db.commit()
    login(client, db, legal)
    resp = client.put(
        f"/admin/v1/licences/{lic.id}/gate",
        json={"gate_flag": True, "reuse_class": "not-a-class", "reason": "x"},
    )
    assert resp.status_code == 400


def test_next_licence_id_deduplicates_on_collision(client, db):
    lic = _gated_licence(db, id_="collide-base")
    # Pre-create the id the reclassification would naturally pick, forcing a numeric suffix.
    taken = Licence(id="collide-base-attribution", name="Taken", reuse_class="attribution")
    db.add(taken)
    db.flush()
    legal = _legal(db)
    db.commit()
    login(client, db, legal)

    resp = client.put(
        f"/admin/v1/licences/{lic.id}/gate",
        json={"gate_flag": False, "reuse_class": "attribution", "reason": "reclassify"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["licence_id"] == "collide-base-attribution-2"


# ------------------------------------------------------------------ extra coverage: costs
def test_get_costs_filters_by_source_id_and_purpose_and_group_by(client, db):
    lic = make_open_licence(db)
    src = _make_source(db, lic, source_id="us.test.costfilter")
    other = _make_source(db, lic, source_id="us.test.costother")
    day = dt.datetime(2026, 9, 11, tzinfo=UTC)
    db.add(
        ModelCall(
            created_at=day,
            purpose="extract",
            source_id=src.id,
            alias="fast",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.01,
        )
    )
    db.add(
        ModelCall(
            created_at=day,
            purpose="adjudicate",
            source_id=other.id,
            alias="careful",
            input_tokens=20,
            output_tokens=10,
            cost_usd=0.02,
        )
    )
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get(
        "/admin/v1/costs",
        params={"source_id": src.id, "purpose": "extract", "group_by": "purpose,day,model_alias"},
    )
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert len(rows) == 1
    assert rows[0]["source_id"] is None  # not part of the requested group_by
    assert rows[0]["purpose"] == "extract"
    assert rows[0]["model_alias"] == "fast"


def test_get_costs_rejects_bad_purpose_and_group_by(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/costs", params={"purpose": "not-a-purpose"})
    assert resp.status_code == 400
    resp2 = client.get("/admin/v1/costs", params={"group_by": "not-a-field"})
    assert resp2.status_code == 400


def test_get_costs_flags_high_cost_sources(client, db):
    lic = make_open_licence(db)
    _make_source(db, lic, source_id="us.test.flagged", cost_per_changed_record_30d=1.25)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/costs")
    assert resp.status_code == 200
    assert "us.test.flagged" in resp.json()["data"]["flagged_sources"]


# ------------------------------------------------------------------ extra coverage: audit filters
def test_list_audit_filters_by_since_seq_and_actor_and_subject_id(client, db):
    from services.api.audit import record_audit_event

    operator = _operator(db)
    e1 = record_audit_event(
        db,
        subject_type="account",
        subject_id=operator.account_id,
        event_type="admin_edit",
        actor=operator,
        reason="first",
        before={},
        after={"n": 1},
    )
    record_audit_event(
        db,
        subject_type="account",
        subject_id=operator.account_id,
        event_type="admin_edit",
        actor=operator,
        reason="second",
        before={},
        after={"n": 2},
    )
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/audit", params={"since": e1.seq})
    reasons = [r["reason"] for r in resp.json()["data"]]
    assert "second" in reasons and "first" not in reasons

    resp2 = client.get(
        "/admin/v1/audit", params={"since": "2020-01-01T00:00:00Z", "actor_user_id": operator.public_id}
    )
    assert resp2.status_code == 200
    assert len(resp2.json()["data"]) >= 2

    resp3 = client.get("/admin/v1/audit", params={"actor_user_id": "usr_doesnotexist00"})
    assert resp3.json()["data"] == []


def test_list_audit_subject_id_filter_resolves_source_ids(client, db):
    from services.api.admin_sources import _source_subject_uuid
    from services.api.audit import record_audit_event

    lic = make_open_licence(db, id_="audit-subj-lic")
    src = _make_source(db, lic, source_id="us.test.auditsubj")
    operator = _operator(db)
    record_audit_event(
        db,
        subject_type="source",
        subject_id=_source_subject_uuid(src.id),
        event_type="admin_edit",
        actor=operator,
        reason="direct",
        before={"source_id": src.id},
        after={"source_id": src.id, "paused": True},
    )
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/audit", params={"subject_id": src.id})
    reasons = [r["reason"] for r in resp.json()["data"]]
    assert "direct" in reasons
