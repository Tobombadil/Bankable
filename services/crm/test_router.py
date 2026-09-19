"""Router tests for `POST /webhooks/attio` and `POST /admin/v1/leads` (docs/34 §5; US-403).

Builds a standalone FastAPI app carrying only this module's router plus the `ProblemError`
handler (task's option B — "build a small FastAPI app that includes only your router for tests")
rather than mutating the shared `services.api.app.app`, so this suite never depends on load order
with the other agents' concurrent work in `services/billing/` and `web/`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.auth import create_session, hash_password
from services.api.deps import get_db
from services.api.errors import ProblemError, problem_exception_handler
from services.crm.fake import InMemoryCrm
from services.crm.router import router
from services.db.models import Account, Event, Match, Opportunity, Organization, Proposal, User
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.sor.ports import CompanyUpsert, SorRejected, SorUnavailable
from services.sor.wiring import get_crm_port

UTC = dt.UTC


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ProblemError, problem_exception_handler)
    app.include_router(router)
    return app


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def fake_crm() -> InMemoryCrm:
    return InMemoryCrm()


@pytest.fixture()
def client(db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm) -> Iterator[TestClient]:
    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: fake_crm
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------------- fixtures
def make_account(session: Session, *, entitlement: str = "admin", name: str = "Ops") -> Account:
    account = Account(public_id="", name=name, kind="organization", entitlement=entitlement)
    session.add(account)
    session.flush()
    account.public_id = public_id("acc", account.id)
    session.flush()
    return account


def make_user(
    session: Session, account: Account, *, email: str = "ops@example.com", role: str = "operator"
) -> User:
    user = User(
        public_id="",
        account_id=account.id,
        email=email,
        password_hash=hash_password("correct horse battery staple"),
        name="Ops User",
        role=role,
        auth_provider="password",
    )
    session.add(user)
    session.flush()
    user.public_id = public_id("usr", user.id)
    session.flush()
    return user


def login(client: TestClient, session_db: Session, user: User) -> None:
    _row, cookie_value = create_session(session_db, user)
    session_db.commit()
    client.cookies.set("session", cookie_value)


def make_org(
    session: Session,
    *,
    name: str = "Acme Power LLC",
    website: str | None = "https://www.acme-power.example/about",
) -> Organization:
    org = Organization(
        public_id="",
        slug="",
        name_canonical=name,
        name_normalised=name.lower(),
        type="developer",
        country="US",
        website=website,
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = slugify(name)
    session.flush()
    return org


def make_proposal(
    session: Session, *, sponsor: Organization | None = None, name: str = "Test Storage Project"
) -> Proposal:
    prop = Proposal(
        public_id="",
        slug="",
        kind="storage",
        name_canonical=name,
        jurisdiction="US-TX",
        lifecycle_state="filed",
        publish_state="public",
        min_reuse_class="open",
        sponsor_org_id=sponsor.id if sponsor else None,
    )
    session.add(prop)
    session.flush()
    prop.public_id = public_id("prop", prop.id)
    prop.slug = slugify(name)
    session.flush()
    return prop


def make_opportunity(session: Session, *, title: str = "Test RFP") -> Opportunity:
    opp = Opportunity(
        public_id="",
        slug="",
        kind="rfp",
        title=title,
        jurisdiction="US-AZ",
        status="open",
        publish_state="public",
        min_reuse_class="open",
    )
    session.add(opp)
    session.flush()
    opp.public_id = public_id("opp", opp.id)
    opp.slug = slugify(title)
    session.flush()
    return opp


def make_match(
    session: Session,
    proposal: Proposal,
    opportunity: Opportunity,
    *,
    score: float = 0.85,
    status: str = "active",
) -> Match:
    match = Match(
        proposal_id=proposal.id,
        opportunity_id=opportunity.id,
        score=score,
        rationale={},
        rationale_text="Strong geographic and technology overlap. Timing also lines up well.",
        rule_set_version="v1",
        status=status,
    )
    session.add(match)
    session.flush()
    return match


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- POST /admin/v1/leads
def test_lead_requires_admin_session(client: TestClient, db: Session) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    db.commit()

    resp = client.post("/admin/v1/leads", json={"match_id": public_id("mat", match.id), "reason": "hand off"})
    assert resp.status_code == 401


def test_lead_happy_path_creates_signal_deal_and_audit_event(
    client: TestClient, db: Session, fake_crm: InMemoryCrm
) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    match_public_id = public_id("mat", match.id)
    resp = client.post(
        "/admin/v1/leads",
        json={"match_id": match_public_id, "reason": "hand off to sales", "notes": "warm intro"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["match_id"] == match_public_id
    assert data["sor_kind"] == "attio_fake"
    assert data["crm_lead_ref"]
    assert data["open_in_crm_url"] is None
    assert data["event_id"].startswith("evt_")

    # the CRM fake actually recorded the hand-off
    assert "acme-power.example" in fake_crm.companies
    assert len(fake_crm.signals) == 1
    assert data["crm_lead_ref"] in fake_crm.deals

    # the platform stored only the reference, plus an audit event
    db.expire_all()
    refreshed = db.get(Match, match.id)
    assert refreshed is not None
    assert refreshed.crm_lead_ref == data["crm_lead_ref"]
    events = db.query(Event).filter_by(subject_type="match", event_type="lead_created").all()
    assert len(events) == 1
    assert events[0].reason == "hand off to sales"
    assert events[0].after is not None
    assert events[0].after["notes"] == "warm intro"


def test_lead_unknown_match_is_404(client: TestClient, db: Session) -> None:
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    resp = client.post("/admin/v1/leads", json={"match_id": "mat_doesnotexist0000000000", "reason": "x"})
    assert resp.status_code == 404


def test_lead_malformed_match_id_is_404(client: TestClient, db: Session) -> None:
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    resp = client.post("/admin/v1/leads", json={"match_id": "not-a-match-id", "reason": "x"})
    assert resp.status_code == 404


def test_lead_account_id_must_be_a_string(client: TestClient, db: Session) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    resp = client.post(
        "/admin/v1/leads",
        json={"match_id": public_id("mat", match.id), "reason": "x", "account_id": 123},
    )
    assert resp.status_code == 400


def test_lead_unknown_account_id_is_404(client: TestClient, db: Session) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    resp = client.post(
        "/admin/v1/leads",
        json={
            "match_id": public_id("mat", match.id),
            "reason": "x",
            "account_id": "acc_doesnotexist0000000000",
        },
    )
    assert resp.status_code == 404


def test_lead_links_a_named_customer_account(client: TestClient, db: Session, fake_crm: InMemoryCrm) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    customer_account = make_account(db, entitlement="pro", name="Customer Co")
    operator_account = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, operator_account, email="ops2@example.com")
    db.commit()
    login(client, db, operator)

    resp = client.post(
        "/admin/v1/leads",
        json={
            "match_id": public_id("mat", match.id),
            "reason": "x",
            "account_id": customer_account.public_id,
        },
    )
    assert resp.status_code == 201, resp.text
    row = fake_crm.companies["acme-power.example"]
    assert row.platform_account_id == customer_account.public_id


def test_lead_already_linked_is_409(client: TestClient, db: Session) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    match_public_id = public_id("mat", match.id)
    first = client.post("/admin/v1/leads", json={"match_id": match_public_id, "reason": "x"})
    assert first.status_code == 201
    second = client.post("/admin/v1/leads", json={"match_id": match_public_id, "reason": "x again"})
    assert second.status_code == 409


class _UnavailableCrm(InMemoryCrm):
    def create_lead_signal(self, signal: object) -> object:  # type: ignore[override]
        raise SorUnavailable("attio is down")


class _RejectingCrm(InMemoryCrm):
    def create_deal(self, deal: object) -> object:  # type: ignore[override]
        raise SorRejected("attio said no")


def test_lead_sor_rejected_is_409(db_sessionmaker: sessionmaker[Session], db: Session) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()

    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: _RejectingCrm()
    with TestClient(app) as rejecting_client:
        login(rejecting_client, db, operator)
        resp = rejecting_client.post(
            "/admin/v1/leads", json={"match_id": public_id("mat", match.id), "reason": "x"}
        )
    assert resp.status_code == 409


def test_lead_sor_unavailable_is_503(db_sessionmaker: sessionmaker[Session], db: Session) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()

    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: _UnavailableCrm()
    with TestClient(app) as unavailable_client:
        login(unavailable_client, db, operator)
        resp = unavailable_client.post(
            "/admin/v1/leads", json={"match_id": public_id("mat", match.id), "reason": "x"}
        )
    assert resp.status_code == 503
    assert resp.json()["code"] == "sor_unavailable"
    assert resp.json()["detail"] == "The CRM could not be reached; try again shortly."
    assert "attio is down" not in resp.text  # vendor message stays in logs, never the response


def test_lead_missing_reason_is_400(client: TestClient, db: Session) -> None:
    org = make_org(db)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    resp = client.post("/admin/v1/leads", json={"match_id": public_id("mat", match.id)})
    assert resp.status_code == 400


def test_lead_with_no_sponsor_website_still_creates_a_deal_and_lands_unmatched(
    client: TestClient, db: Session, fake_crm: InMemoryCrm
) -> None:
    org = make_org(db, website=None)
    proposal = make_proposal(db, sponsor=org)
    opportunity = make_opportunity(db)
    match = make_match(db, proposal, opportunity)
    operator_account = make_account(db, entitlement="admin")
    operator = make_user(db, operator_account)
    db.commit()
    login(client, db, operator)

    resp = client.post("/admin/v1/leads", json={"match_id": public_id("mat", match.id), "reason": "x"})
    assert resp.status_code == 201, resp.text
    assert len(fake_crm.unmatched) == 1
    assert fake_crm.companies == {}


# ---------------------------------------------------------------------------- POST /webhooks/attio
def test_webhook_updates_cached_account_name(client: TestClient, db: Session, fake_crm: InMemoryCrm) -> None:
    ref = fake_crm.upsert_company(CompanyUpsert(domain="acme.example"))
    fake_crm.companies["acme.example"].name = "Acme Power (renamed)"
    account = Account(
        public_id="",
        name="Acme Power",
        kind="organization",
        entitlement="pro",
        sor_kind="attio_fake",
        sor_ref=ref.sor_ref,
    )
    db.add(account)
    db.flush()
    account.public_id = public_id("acc", account.id)
    db.commit()

    payload = {"event_type": "record.updated", "id": {"object_id": "companies", "record_id": ref.sor_ref}}
    body = json.dumps(payload).encode()
    sig = _sign("test-webhook-secret", body)
    resp = client.post("/webhooks/attio", content=body, headers={"Attio-Signature": sig})
    assert resp.status_code == 200
    assert resp.json() == {"received": 1, "applied": 1}

    db.expire_all()
    refreshed = db.get(Account, account.id)
    assert refreshed is not None
    assert refreshed.name == "Acme Power (renamed)"


def test_webhook_lead_signal_updated_is_received_but_not_applied(client: TestClient) -> None:
    payload = {"event_type": "record.updated", "id": {"object_id": "lead_signals", "record_id": "sig_1"}}
    body = json.dumps(payload).encode()
    sig = _sign("test-webhook-secret", body)
    resp = client.post("/webhooks/attio", content=body, headers={"Attio-Signature": sig})
    assert resp.status_code == 200
    assert resp.json() == {"received": 1, "applied": 0}


def test_webhook_unknown_event_is_not_received(client: TestClient) -> None:
    payload = {"event_type": "record.deleted", "id": {"object_id": "companies", "record_id": "cmp_1"}}
    body = json.dumps(payload).encode()
    sig = _sign("test-webhook-secret", body)
    resp = client.post("/webhooks/attio", content=body, headers={"Attio-Signature": sig})
    assert resp.status_code == 200
    assert resp.json() == {"received": 0, "applied": 0}


def test_webhook_bad_signature_is_401(client: TestClient) -> None:
    body = json.dumps(
        {"event_type": "record.updated", "id": {"object_id": "companies", "record_id": "x"}}
    ).encode()
    resp = client.post("/webhooks/attio", content=body, headers={"Attio-Signature": "0" * 64})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"
    assert resp.json().get("detail") is None  # no exception text leaked into the response


def test_webhook_missing_signature_is_401(client: TestClient) -> None:
    body = b'{"event_type": "record.updated"}'
    resp = client.post("/webhooks/attio", content=body)
    assert resp.status_code == 401


class _WebhookUnavailableCrm(InMemoryCrm):
    def get_company(self, ref: object) -> object:  # type: ignore[override]
        raise SorUnavailable("attio is down")


def test_webhook_sor_unavailable_is_503(db_sessionmaker: sessionmaker[Session]) -> None:
    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: _WebhookUnavailableCrm()
    payload = {"event_type": "record.updated", "id": {"object_id": "companies", "record_id": "cmp_1"}}
    body = json.dumps(payload).encode()
    sig = _sign("test-webhook-secret", body)
    with TestClient(app) as unavailable_client:
        resp = unavailable_client.post("/webhooks/attio", content=body, headers={"Attio-Signature": sig})
    assert resp.status_code == 503
    assert resp.json()["code"] == "sor_unavailable"
    assert resp.json()["detail"] == "The CRM could not be reached; try again shortly."
    assert "attio is down" not in resp.text
