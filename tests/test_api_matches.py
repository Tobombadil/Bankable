"""The match routes (`services/api/matches.py`; docs/10 US-401-403; docs/23 §3.1, §3.2): the six
operations validate against `api/openapi.yaml`, a match is visible only when both sides are,
dismissal is per user and never global, the events land in both timelines, the intake approval
computes matches, and a computed match hands off to the CRM through `POST /admin/v1/leads`.

Two states of the rule set's `publish` gate (`data/match_rules.yaml`): the committed rule set is
withheld (`publish: false`), so most tests below run with the gate opened by the autouse
`published` fixture and a rule set copy with `publish=True`; the "publish gate closed" section at
the end runs against the committed state and pins what every non-operator caller sees then."""

from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api import matches as matches_mod
from services.api.conftest import (
    make_attribution_licence,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.crm.fake import InMemoryCrm
from services.db.models import Match, MatchDismissal, Opportunity, Proposal, Source
from services.ids import public_id
from services.match.rules import RuleSet, load_rules
from services.match.run import run_matches
from services.sor.wiring import get_crm_port
from tests.conftest import login, make_account, make_api_key, make_user
from tests.test_api_contract import OPENAPI_PATH, assert_valid

UTC = dt.UTC


#: The committed rule set with its display gate opened -- what `publish: true` would mean.
PUBLISHED_RULES: RuleSet = dataclasses.replace(load_rules(), publish=True)


@pytest.fixture(autouse=True)
def published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matches_mod, "matches_published", lambda: True)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    with pathlib.Path(OPENAPI_PATH).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _proposal(
    db: Session, source: Source, suffix: str, *, technology: str = "storage", state: str = "US-TX"
) -> Proposal:
    prop = make_visible_proposal(db, source, public_id_suffix=suffix)
    prop.technology, prop.jurisdiction, prop.capacity_mw, prop.lifecycle_state = (
        technology,
        state,
        200.0,
        "studied",
    )
    return prop


def _rfp(db: Session, source: Source, suffix: str, *, technologies: list[str] | None = None) -> Opportunity:
    opp = make_visible_opportunity(db, source, public_id_suffix=suffix)
    opp.technologies = technologies or ["bess"]
    opp.jurisdiction, opp.capacity_sought_mw = "US-TX", 500.0
    opp.due_at = dt.datetime.now(UTC) + dt.timedelta(days=45)
    return opp


def _seed(db: Session, rules: RuleSet) -> dict[str, Any]:
    """Three storage proposals in TX against one storage RFP (three matches), plus one solar
    proposal against a solar RFP (a fourth) -- every record visible on every tier."""
    attr = make_attribution_licence(db)
    opn = make_open_licence(db)
    queue = make_public_source(db, attr, id_="us.test.queue")
    notices = make_public_source(db, opn, id_="us.test.notices")
    props = [_proposal(db, queue, str(i)) for i in (1, 2, 3)]
    solar = _proposal(db, queue, "4", technology="solar")
    rfp = _rfp(db, notices, "1")
    solar_rfp = _rfp(db, notices, "2", technologies=["solar_pv"])
    db.flush()
    report = run_matches(db, full=True, rules=rules)
    db.commit()
    assert report.added == 4
    return {"props": props, "solar": solar, "rfp": rfp, "solar_rfp": solar_rfp, "queue": queue}


@pytest.fixture()
def seeded(db: Session) -> dict[str, Any]:
    return _seed(db, PUBLISHED_RULES)


def _pro(db: Session, client: TestClient, *, email: str = "ana@example.com", entitlement: str = "pro") -> Any:
    account = make_account(db, entitlement=entitlement, name=f"Account {email}")
    user = make_user(db, account, email=email)
    db.commit()
    login(client, db, user)
    return account, user


def _match_id(db: Session, proposal: Proposal) -> str:
    match = db.scalars(select(Match).where(Match.proposal_id == proposal.id)).one()
    return public_id("mat", match.id)


# -------------------------------------------------------------------------------- public lists
def test_proposal_matches_are_public_and_match_the_schema(
    client: TestClient, seeded: dict[str, Any], spec: dict[str, Any]
) -> None:
    prop = seeded["props"][0]
    resp = client.get(f"/v1/proposals/{prop.public_id}/matches")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid(spec, "MatchListResponse", body)
    (row,) = body["data"]
    assert row["opportunity"]["public_id"] == seeded["rfp"].public_id
    assert row["rationale_text"] == "storage, TX, 50–550 MW, due in 45 days"
    assert row["score"] == 1.0 and row["rule_set_version"] == "match-rules@v1"
    assert row["first_matched_at"] and row["status"] == "active"
    assert "dismissed_by_me" not in row and "crm_lead_ref" not in row
    assert row["proposal"]["provenance"][0]["source_id"] == "us.test.queue"
    assert row["opportunity"]["provenance"][0]["source_id"] == "us.test.notices"
    sources = {s["source_id"] for s in body["licence_summary"]["sources"]}
    assert sources == {"us.test.queue", "us.test.notices"}
    assert body["meta"]["tier"] == "public"


def test_opportunity_matches_twin(client: TestClient, seeded: dict[str, Any], spec: dict[str, Any]) -> None:
    resp = client.get(f"/v1/opportunities/{seeded['rfp'].public_id}/matches")
    assert resp.status_code == 200, resp.text
    assert_valid(spec, "MatchListResponse", resp.json())
    assert [r["score"] for r in resp.json()["data"]] == [1.0, 1.0, 1.0]
    assert client.get("/v1/opportunities/opp_NOPE000000/matches").status_code == 404


def test_record_lists_reject_what_the_spec_does_not_name(client: TestClient, seeded: dict[str, Any]) -> None:
    prop = seeded["props"][0]
    assert client.get("/v1/proposals/prop_NOPE000000/matches").status_code == 404
    assert (
        client.get(f"/v1/proposals/{prop.public_id}/matches?sort=score").json()["code"] == "unknown_parameter"
    )
    bad = client.get(f"/v1/proposals/{prop.public_id}/matches?status=gone")
    assert bad.status_code == 400 and bad.json()["code"] == "validation_error"
    removed = client.get(f"/v1/proposals/{prop.public_id}/matches?status=removed")
    assert removed.status_code == 200 and removed.json()["data"] == []


def test_a_match_is_visible_only_when_both_sides_are(
    client: TestClient, db: Session, seeded: dict[str, Any]
) -> None:
    prop, rfp = seeded["props"][0], seeded["rfp"]
    rfp.publish_state = "pending_review"
    db.commit()
    resp = client.get(f"/v1/proposals/{prop.public_id}/matches")
    assert resp.status_code == 200 and resp.json()["data"] == []  # the proposal is fine; the match is not
    assert client.get(f"/v1/opportunities/{rfp.public_id}/matches").status_code == 404

    rfp.publish_state = "public"
    seeded["queue"].publish_state = "ingest_only"  # the proposal side's only source
    db.commit()
    assert client.get(f"/v1/opportunities/{rfp.public_id}/matches").json()["data"] == []


def test_match_events_land_in_both_timelines(client: TestClient, db: Session, seeded: dict[str, Any]) -> None:
    prop, rfp = seeded["props"][0], seeded["rfp"]
    p_events = client.get(f"/v1/proposals/{prop.public_id}/events?event_type=match_added").json()["data"]
    o_events = client.get(f"/v1/opportunities/{rfp.public_id}/events?event_type=match_added").json()["data"]
    assert len(p_events) == 1 and len(o_events) == 3
    assert p_events[0]["after"]["counterpart"]["public_id"] == rfp.public_id
    assert p_events[0]["provenance"]["source_id"] == "us.test.notices"

    rfp.status = "closed"
    db.commit()
    run_matches(db, rules=PUBLISHED_RULES)
    db.commit()
    removed = client.get(f"/v1/proposals/{prop.public_id}/events?event_type=match_removed").json()["data"]
    assert removed[0]["after"]["reason"] == "rules failed: timing"


# ------------------------------------------------------------------------------------ Pro list
def test_list_matches_needs_pro(client: TestClient, db: Session, seeded: dict[str, Any]) -> None:
    assert client.get("/v1/matches").status_code == 401
    _pro(db, client, entitlement="public")
    resp = client.get("/v1/matches")
    assert resp.status_code == 403 and resp.json()["code"] == "forbidden_tier"


def test_list_matches_for_pro(
    client: TestClient, db: Session, seeded: dict[str, Any], spec: dict[str, Any]
) -> None:
    _pro(db, client)
    resp = client.get("/v1/matches?include=count")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid(spec, "MatchListResponse", body)
    assert len(body["data"]) == 4 and body["meta"]["total"] == 4 and body["meta"]["tier"] == "pro"
    assert all(row["dismissed_by_me"] is False for row in body["data"])
    assert "RateLimit-Limit" in resp.headers
    assert client.get("/v1/matches?bogus=1").json()["code"] == "unknown_parameter"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("technology=solar", 1),
        ("technology=bess", 3),
        ("technology=wind", 0),
        ("jurisdiction=US-TX", 4),
        ("jurisdiction=US-CA", 0),
        ("score[gte]=0.9", 4),
        ("status=removed", 0),
        ("status=active,removed", 4),
        ("updated_since=2000-01-01T00:00:00Z", 4),
        ("updated_since=2999-01-01", 0),
        ("sort=first_matched_at", 4),
        ("sort=-first_matched_at", 4),
    ],
)
def test_list_matches_filters(
    client: TestClient, db: Session, seeded: dict[str, Any], query: str, expected: int
) -> None:
    _pro(db, client)
    resp = client.get(f"/v1/matches?{query}")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == expected


def test_list_matches_side_filters(client: TestClient, db: Session, seeded: dict[str, Any]) -> None:
    _pro(db, client)
    prop, solar_rfp = seeded["props"][0], seeded["solar_rfp"]
    assert len(client.get(f"/v1/matches?proposal_id={prop.public_id}").json()["data"]) == 1
    assert len(client.get(f"/v1/matches?opportunity_id={solar_rfp.public_id}").json()["data"]) == 1


@pytest.mark.parametrize(
    "query",
    ["score[gte]=high", "score[gte]=2", "updated_since=yesterday", "include_dismissed=maybe", "sort=name"],
)
def test_list_matches_bad_parameters(
    client: TestClient, db: Session, seeded: dict[str, Any], query: str
) -> None:
    _pro(db, client)
    resp = client.get(f"/v1/matches?{query}")
    assert resp.status_code == 400 and resp.json()["code"] == "validation_error"


def test_list_matches_paginates_on_score(client: TestClient, db: Session, seeded: dict[str, Any]) -> None:
    _pro(db, client)
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        url = "/v1/matches?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url).json()
        seen.extend(r["match_id"] for r in page["data"])
        cursor = page["page"]["next_cursor"]
        if not page["page"]["has_more"]:
            break
    assert len(seen) == 4 and len(set(seen)) == 4


@pytest.mark.parametrize(
    "cursor",
    [
        "%%%",
        "eyJ2IjogMC45LCAiaWQiOiAibm90LWEtdXVpZCJ9",
        "eyJ2IjogIm5vcGUiLCAiaWQiOiAiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAxIn0",
    ],
)
def test_tampered_cursor_is_400(client: TestClient, db: Session, seeded: dict[str, Any], cursor: str) -> None:
    _pro(db, client)
    resp = client.get(f"/v1/matches?cursor={cursor}")
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_cursor"


# ---------------------------------------------------------------------------------- match detail
def test_get_match(client: TestClient, db: Session, seeded: dict[str, Any], spec: dict[str, Any]) -> None:
    match_id = _match_id(db, seeded["props"][0])
    assert client.get(f"/v1/matches/{match_id}").status_code == 401
    _pro(db, client)
    resp = client.get(f"/v1/matches/{match_id}")
    assert resp.status_code == 200, resp.text
    assert_valid(spec, "MatchDetailResponse", resp.json())
    assert resp.json()["data"]["match_id"] == match_id
    assert client.get("/v1/matches/mat_NOPE0000!!").status_code == 404
    assert client.get("/v1/matches/prop_0000000000").status_code == 404
    assert client.get("/v1/matches/mat_0000000001").status_code == 404
    assert client.get(f"/v1/matches/{match_id}?x=1").json()["code"] == "unknown_parameter"

    seeded["rfp"].publish_state = "pending_review"
    db.commit()
    assert client.get(f"/v1/matches/{match_id}").status_code == 404  # hidden side: the same 404


# ------------------------------------------------------------------------------------ dismissal
def test_dismissal_is_per_user_and_never_global(
    client: TestClient, db: Session, seeded: dict[str, Any], spec: dict[str, Any]
) -> None:
    prop = seeded["props"][0]
    match_id = _match_id(db, prop)
    match_row = db.scalars(select(Match).where(Match.proposal_id == prop.id)).one()
    before = (match_row.status, float(match_row.score), match_row.last_evaluated_at)
    _pro(db, client)

    first = client.post(f"/v1/matches/{match_id}/dismiss")
    assert first.status_code == 200, first.text
    assert_valid(spec, "MatchDetailResponse", first.json())
    assert first.json()["data"]["dismissed_by_me"] is True
    assert (
        client.post(f"/v1/matches/{match_id}/dismiss", headers={"Idempotency-Key": "dismiss1"}).status_code
        == 200
    )
    assert len(db.scalars(select(MatchDismissal)).all()) == 1  # idempotent

    listed = {r["match_id"] for r in client.get("/v1/matches").json()["data"]}
    assert match_id not in listed and len(listed) == 3
    with_dismissed = client.get("/v1/matches?include_dismissed=true").json()["data"]
    flagged = {r["match_id"]: r["dismissed_by_me"] for r in with_dismissed}
    assert flagged[match_id] is True and len(flagged) == 4
    # The detail page flags it for this user rather than hiding it.
    on_record = client.get(f"/v1/proposals/{prop.public_id}/matches").json()["data"]
    assert on_record[0]["dismissed_by_me"] is True

    db.refresh(match_row)
    assert (match_row.status, float(match_row.score), match_row.last_evaluated_at) == before

    # Another Pro user still sees it: the dismissal is personal.
    client.cookies.clear()
    _pro(db, client, email="bo@example.com")
    assert match_id in {r["match_id"] for r in client.get("/v1/matches").json()["data"]}

    # The first user restores it; restoring twice is still 204.
    client.cookies.clear()
    first_user = db.scalars(select(MatchDismissal)).one().user_id
    from services.db.models import User

    login(client, db, db.get(User, first_user))  # type: ignore[arg-type]
    assert client.delete(f"/v1/matches/{match_id}/dismiss").status_code == 204
    assert client.delete(f"/v1/matches/{match_id}/dismiss").status_code == 204
    assert match_id in {r["match_id"] for r in client.get("/v1/matches").json()["data"]}
    assert db.scalars(select(MatchDismissal)).all() == []


def test_dismissal_routes_need_pro_and_a_visible_match(
    client: TestClient, db: Session, seeded: dict[str, Any]
) -> None:
    match_id = _match_id(db, seeded["props"][0])
    assert client.post(f"/v1/matches/{match_id}/dismiss").status_code == 401
    assert client.delete(f"/v1/matches/{match_id}/dismiss").status_code == 401
    _pro(db, client)
    assert client.post("/v1/matches/mat_0000000001/dismiss").status_code == 404
    assert client.delete("/v1/matches/mat_0000000001/dismiss").status_code == 404


def test_an_api_key_dismisses_for_the_user_who_created_it(
    client: TestClient, db: Session, seeded: dict[str, Any]
) -> None:
    account = make_account(db, entitlement="api")
    user = make_user(db, account, email="dev@example.com")
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    match_id = _match_id(db, seeded["props"][0])
    resp = client.post(f"/v1/matches/{match_id}/dismiss", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 200, resp.text
    assert db.scalars(select(MatchDismissal)).one().user_id == user.id


# -------------------------------------------------------------------- US-403: the CRM hand-off
def test_a_computed_match_hands_off_to_the_crm(
    client: TestClient, db: Session, seeded: dict[str, Any]
) -> None:
    from services.api.app import app

    fake = InMemoryCrm()
    app.dependency_overrides[get_crm_port] = lambda: fake
    ops = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, ops, email="ops@example.com", role="operator")
    db.commit()
    login(client, db, operator)

    listed = client.get(f"/v1/matches?proposal_id={seeded['props'][0].public_id}").json()["data"]
    assert listed[0]["crm_lead_ref"] is None  # operators see the field; nobody else does
    match_id = listed[0]["match_id"]
    lead = client.post("/admin/v1/leads", json={"match_id": match_id, "reason": "strong fit"})
    assert lead.status_code == 201, lead.text
    ref = lead.json()["data"]["crm_lead_ref"]
    assert ref
    assert client.get(f"/v1/matches/{match_id}").json()["data"]["crm_lead_ref"] == ref
    del app.dependency_overrides[get_crm_port]


# --------------------------------------------------------------------- US-1002 AC2: intake approval
def test_intake_approval_computes_matches(client: TestClient, db: Session, seeded: dict[str, Any]) -> None:
    accepted = client.post(
        "/v1/intake/proposals",
        json={
            "project_name": "Sunrise Storage",
            "kind": "storage",
            "technology": "storage",
            "jurisdiction": "US-TX",
            "lifecycle_state": "announced",
            "sponsor_name": "Sunrise Power LLC",
            "capacity_mw": 100,
            "contact": {"name": "Jamie Rivera", "email": "jamie@example.com"},
            "consent": True,
            "captcha_token": "test-token",
        },
    )
    assert accepted.status_code == 202, accepted.text
    ops = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, ops, email="ops@example.com", role="operator")
    db.commit()
    login(client, db, operator)
    decided = client.post(
        f"/admin/v1/tasks/{accepted.json()['task_id']}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "verified"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["matches_computed"] == 1
    created = db.scalar(select(Proposal).where(Proposal.name_canonical == "Sunrise Storage"))
    assert created is not None
    # Pending review: matched, but invisible on the public detail list of the RFP.
    rfp_matches = client.get(f"/v1/opportunities/{seeded['rfp'].public_id}/matches").json()["data"]
    assert created.public_id not in {r["proposal"]["public_id"] for r in rfp_matches}


# ------------------------------------------------------------------ publish gate closed (committed)
@pytest.fixture()
def gated(db: Session, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The committed state: `match-rules@v1` has `publish: false`. Matches are computed with the
    committed rule set (so the events carry its stamps) and the API reads the committed flag."""
    monkeypatch.setattr(matches_mod, "matches_published", lambda: load_rules().publish)
    assert load_rules().publish is False
    return _seed(db, load_rules())


def _withheld_ok(body: dict[str, Any], spec: dict[str, Any]) -> None:
    assert_valid(spec, "MatchListResponse", body)
    assert body["data"] == [] and body["page"]["has_more"] is False
    assert body["matches_withheld"]["reason"] == "pending_evaluation"
    assert body["matches_withheld"]["rule_set_version"] == "match-rules@v1"


def test_gate_closed_record_lists_are_empty_and_say_why(
    client: TestClient, gated: dict[str, Any], spec: dict[str, Any]
) -> None:
    prop, rfp = gated["props"][0], gated["rfp"]
    _withheld_ok(client.get(f"/v1/proposals/{prop.public_id}/matches").json(), spec)
    _withheld_ok(client.get(f"/v1/opportunities/{rfp.public_id}/matches").json(), spec)
    # Parameters are still validated, and an invisible record is still a 404.
    assert client.get(f"/v1/proposals/{prop.public_id}/matches?status=gone").status_code == 400
    assert client.get("/v1/proposals/prop_NOPE000000/matches").status_code == 404


def test_gate_closed_pro_list_and_by_id_routes(
    client: TestClient, db: Session, gated: dict[str, Any], spec: dict[str, Any]
) -> None:
    match_id = _match_id(db, gated["props"][0])
    _pro(db, client)
    body = client.get("/v1/matches?include=count&include_dismissed=true").json()
    _withheld_ok(body, spec)
    assert body["meta"]["total"] == 0
    assert client.get("/v1/matches?score[gte]=2").status_code == 400
    assert client.get("/v1/matches?cursor=%25%25%25").json()["code"] == "invalid_cursor"
    assert client.get(f"/v1/matches/{match_id}").status_code == 404
    assert client.post(f"/v1/matches/{match_id}/dismiss").status_code == 404
    assert client.delete(f"/v1/matches/{match_id}/dismiss").status_code == 404
    assert db.scalars(select(MatchDismissal)).all() == []


def test_gate_closed_operators_see_everything_and_hand_off(
    client: TestClient, db: Session, gated: dict[str, Any], spec: dict[str, Any]
) -> None:
    from services.api.app import app

    fake = InMemoryCrm()
    app.dependency_overrides[get_crm_port] = lambda: fake
    ops = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, ops, email="ops@example.com", role="operator")
    db.commit()
    login(client, db, operator)
    body = client.get("/v1/matches").json()
    assert_valid(spec, "MatchListResponse", body)
    assert len(body["data"]) == 4 and "matches_withheld" not in body
    record = client.get(f"/v1/proposals/{gated['props'][0].public_id}/matches").json()
    assert len(record["data"]) == 1 and "matches_withheld" not in record
    match_id = body["data"][0]["match_id"]
    lead = client.post("/admin/v1/leads", json={"match_id": match_id, "reason": "reviewed by hand"})
    assert lead.status_code == 201, lead.text
    assert (
        client.get(f"/v1/matches/{match_id}").json()["data"]["crm_lead_ref"]
        == lead.json()["data"]["crm_lead_ref"]
    )
    del app.dependency_overrides[get_crm_port]


def test_gate_closed_events_never_surface_anywhere(
    client: TestClient, db: Session, gated: dict[str, Any]
) -> None:
    rfp = gated["rfp"]
    rfp.status = "closed"
    db.commit()
    assert run_matches(db).removed == 3  # a gated removal too
    db.commit()
    from services.db.models import Event

    match_events = list(
        db.scalars(select(Event).where(Event.event_type.in_(["match_added", "match_removed"])))
    )
    assert len(match_events) == 14  # 4 added x 2 sides + 3 removed x 2 sides
    assert all(e.public_at is None and e.published_at is None for e in match_events)
    event_ids = {public_id("evt", e.id) for e in match_events}

    def surfaced() -> set[str]:
        seen: set[str] = set()
        for row in client.get("/v1/events?limit=200").json()["data"]:
            seen.add(row["id"])
        for prop in gated["props"]:
            seen.update(r["id"] for r in client.get(f"/v1/proposals/{prop.public_id}/events").json()["data"])
        seen.update(r["id"] for r in client.get(f"/v1/opportunities/{rfp.public_id}/events").json()["data"])
        return seen

    assert not surfaced() & event_ids
    for fmt in ("rss", "json"):
        feed = client.get(f"/feeds/events.{fmt}")
        assert feed.status_code == 200
        assert "match_added" not in feed.text and "match_removed" not in feed.text
    some_id = next(iter(event_ids))
    assert client.get(f"/v1/events/{some_id}").status_code == 404

    _pro(db, client)  # nor on the Pro tier
    assert not surfaced() & event_ids
