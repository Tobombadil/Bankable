"""The private-projects API (docs/19 §6.1; ADR 0009): every rule of §5.6 this lane owns, at the
HTTP boundary, against the real app.

Each user gets their own `TestClient` (one cookie jar each) with an `Origin` naming the test host, as
a browser on the site would send. Public records come from `services/api/conftest.py`'s factories.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from starlette.requests import Request

from services.api import my_projects, ui_events, visibility
from services.api.app import app
from services.api.auth import create_session
from services.api.conftest import (
    make_asset,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.errors import ERROR_CODES
from services.db.models import (
    Event,
    IdempotencyRecord,
    InterconnectionPoint,
    Licence,
    Proposal,
    ProposalSource,
    Source,
    User,
)
from services.ids import public_id
from services.posture import publishable_reuse_classes
from services.projects import authz
from services.projects import models as pm
from tests.conftest import make_account, make_api_key, make_user
from tests.test_api_contract import assert_valid

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ORIGIN = "http://testserver"
MISSING_PROJECT = "prj_0000000000000000000000000"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-my-projects")


Browser = Callable[..., TestClient]


@pytest.fixture()
def browser(client: TestClient, db: Session) -> Iterator[Browser]:
    """A signed-in browser per user: its own cookie jar, the site's `Origin`. `raise_errors=False`
    gives a tab that receives a `500` as a browser would; `base_url` names the host it calls."""
    opened: list[TestClient] = []

    def _open(user: User, *, raise_errors: bool = True, base_url: str = ORIGIN) -> TestClient:
        _row, cookie = create_session(db, user)
        db.commit()
        tab = TestClient(app, base_url=base_url, raise_server_exceptions=raise_errors)
        tab.headers["Origin"] = base_url
        tab.cookies.set("session", cookie)
        opened.append(tab)
        return tab

    yield _open
    for tab in opened:
        tab.close()


UserFactory = Callable[..., User]


@pytest.fixture()
def person(db: Session) -> UserFactory:
    counter = iter(range(1, 1000))

    def _make(email: str | None = None, *, name: str | None = None, verified: bool = True) -> User:
        n = next(counter)
        account = make_account(db, entitlement="public", name=f"Account {n}")
        user = make_user(db, account, email=email or f"person{n}@example.org")
        user.name = name or f"Person {n}"
        user.email_verified_at = dt.datetime(2026, 10, 1, tzinfo=UTC) if verified else None
        db.commit()
        return user

    return _make


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """An open queue source, a placed proposal on it, an open RFP and a retiring plant."""
    licence = make_open_licence(db)
    queue = make_public_source(db, licence)
    queue.operator = "Test ISO"
    location = make_location(db, queue, licence, geom=(-97.74, 30.27), precision="exact")
    proposal = make_visible_proposal(db, queue, location=location)
    proposal.technology = "solar"
    opportunity = make_visible_opportunity(db, queue)
    plant = make_asset(db, queue, licence, status="retiring", name="Old Coal Plant")
    db.commit()
    return {
        "licence": licence,
        "source": queue,
        "proposal": proposal,
        "opportunity": opportunity,
        "plant": plant,
    }


def _create(tab: TestClient, *, title: str | None = "North Ridge Solar", **body: Any) -> dict[str, Any]:
    """A project; `title=None` leaves the title out, so a starting record prefills it."""
    if title is not None:
        body["title"] = title
    resp = tab.post("/v1/my/projects", json=body)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()["data"]
    return data


def _invite_and_join(
    owner_tab: TestClient, project: str, invitee_tab: TestClient, email: str, role: str
) -> None:
    created = owner_tab.post(f"/v1/my/projects/{project}/invitations", json={"email": email, "role": role})
    assert created.status_code == 201, created.text
    token = created.json()["data"]["link"].split("t=", 1)[1]
    accepted = invitee_tab.post("/v1/my/invitations/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text


# ===================================================================================== §5.6 rules
def _depends_on(route: APIRoute, predicate: Callable[[Any], bool]) -> bool:
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is not None and predicate(dependant.call):
            return True
        stack.extend(dependant.dependencies)
    return False


def test_every_project_route_depends_on_project_access() -> None:
    routes = [route for route in my_projects.router.routes if isinstance(route, APIRoute)]
    assert len(routes) == 30, "a route was added or removed: document it in api/openapi.yaml"
    for route in routes:
        assert my_projects.is_private_path(route.path), route.path
        assert _depends_on(route, lambda call: call is my_projects.private_request_guard), route.path
        if "{project_id}" in route.path:
            assert _depends_on(route, lambda call: isinstance(call, authz.ProjectAccessDependency)), (
                route.path
            )
        else:
            assert _depends_on(route, lambda call: call is my_projects.signed_in_user), route.path


def test_body_cannot_set_owner_or_project_id(browser: Browser, person: UserFactory, db: Session) -> None:
    owner, other = person(), person()
    tab, other_tab = browser(owner), browser(other)
    mine = _create(tab)
    theirs = _create(other_tab, title="Their project")
    for key, value in (
        ("owner_user_id", other.public_id),
        ("project_id", theirs["public_id"]),
        ("public_id", theirs["public_id"]),
        ("members", [other.public_id]),
        ("point_precision", "exact"),
    ):
        assert tab.post("/v1/my/projects", json={"title": "x", key: value}).status_code == 400, key
        assert tab.patch(f"/v1/my/projects/{mine['public_id']}", json={key: value}).status_code == 400, key
    for path in ("notes", "links", "checklist", "invitations"):
        resp = tab.post(
            f"/v1/my/projects/{mine['public_id']}/{path}", json={"project_id": theirs["public_id"]}
        )
        assert resp.status_code == 400, path
    # Nothing landed in the other project, and the owner of mine is still the caller alone.
    other_project = db.scalar(
        select(pm.PrivateProject).where(pm.PrivateProject.public_id == theirs["public_id"])
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(pm.PrivateProjectNote)
            .where(pm.PrivateProjectNote.project_id == other_project.id)
        )
        == 0
    )
    members = tab.get(f"/v1/my/projects/{mine['public_id']}/members").json()["data"]
    assert [(m["user_public_id"], m["role"]) for m in members] == [(owner.public_id, "owner")]


def test_link_follows_merge(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    tab = browser(person())
    absorbed = make_visible_proposal(db, world["source"], public_id_suffix="7")
    project = _create(tab)
    link = tab.post(
        f"/v1/my/projects/{project['public_id']}/links",
        json={"record_type": "proposal", "public_id": absorbed.public_id, "relation": "comparable"},
    ).json()["data"]
    assert link["state"] == "available"
    absorbed.merged_into_id = world["proposal"].id
    absorbed.publish_state = "unpublished"
    db.commit()
    links = tab.get(f"/v1/my/projects/{project['public_id']}/links").json()["data"]
    assert links[0]["public_id"] == absorbed.public_id, "the stored reference is not rewritten"
    assert links[0]["state"] == "merged"
    assert links[0]["survivor_public_id"] == world["proposal"].public_id
    assert links[0]["record"]["public_id"] == world["proposal"].public_id
    assert links[0]["record"]["name"] == world["proposal"].name_canonical


def test_link_to_hidden_record_shows_unavailable_only(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    tab = browser(person())
    hidden = make_visible_proposal(db, world["source"], public_id_suffix="8")
    hidden.name_canonical = "Quietly Withdrawn Name"
    db.commit()
    project = _create(tab)
    for record_type, record_id in (("proposal", hidden.public_id), ("asset", world["plant"].public_id)):
        added = tab.post(
            f"/v1/my/projects/{project['public_id']}/links",
            json={"record_type": record_type, "public_id": record_id, "relation": "nearby"},
        )
        assert added.status_code == 201, added.text
    hidden.publish_state = "unpublished"
    world["source"].publish_state = "ingest_only"  # takes the plant down too
    db.commit()
    resp = tab.get(f"/v1/my/projects/{project['public_id']}/links")
    for link in resp.json()["data"]:
        assert link["state"] == "unavailable"
        assert link["record"] is None and link["survivor_public_id"] is None
        assert link["message"] == "This record is no longer available"
    for leaked in ("Quietly Withdrawn Name", "Old Coal Plant", world["source"].name, "unpublished"):
        assert leaked not in resp.text
    detail = tab.get(f"/v1/my/projects/{project['public_id']}").json()
    assert detail["licence_summary"]["sources"] == []


def _nc_licence(db: Session) -> Licence:
    lic = Licence(
        id="nc-lic",
        name="Noncommercial grant",
        reuse_class="noncommercial",
        attribution_required=True,
        attribution_text="Source: Test State Agency",
        requires_link_back=True,
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_commercial_use=False,
        gate_flag=False,
        evidence_url="https://example.org/nc",
        evidence_retrieved_at=dt.datetime(2026, 9, 25, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def test_prefill_skips_noncommercial_fields(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/19 §7: under the noncommercial posture a record can be visible with a field a
    `noncommercial` source supplied. That field is left blank, named, and shown live on the link."""
    monkeypatch.setattr(visibility, "PUBLISHABLE_REUSE_CLASSES", publishable_reuse_classes("noncommercial"))
    nc_source = make_public_source(db, _nc_licence(db), id_="us.test.nc_register")
    proposal = world["proposal"]
    now = dt.datetime.now(UTC)
    db.add(
        ProposalSource(
            proposal_id=proposal.id,
            source_id=nc_source.id,
            source_record_id="NC1",
            source_url="https://example.org/nc#1",
            retrieved_at=now,
            licence_id=nc_source.licence_id,
            raw={},
            first_seen=now,
            last_seen=now,
        )
    )
    proposal.min_reuse_class = "noncommercial"
    proposal.capacity_mw = 250.0
    proposal.field_provenance = {
        "name_canonical": {"source_id": world["source"].id},
        "technology": {"source_id": world["source"].id},
        "capacity_mw": {"source_id": nc_source.id},
        "lifecycle_state": {"source_ids": [world["source"].id, nc_source.id]},
    }
    db.commit()
    tab = browser(person())
    preview = tab.post("/v1/my/prefill", json={"record_type": "proposal", "public_id": proposal.public_id})
    assert preview.status_code == 200, preview.text
    values = preview.json()["data"]["values"]
    assert values["title"] == proposal.name_canonical and values["technology"] == "solar"
    assert "capacity_mw" not in values and "stage" not in values
    blank = {row["field"] for row in preview.json()["data"]["left_blank"]}
    assert blank == {"capacity_mw", "stage"}
    created = tab.post(
        "/v1/my/projects", json={"from": {"record_type": "proposal", "public_id": proposal.public_id}}
    )
    data = created.json()["data"]
    assert data["capacity_mw"] is None and data["stage"] == "prospecting"
    assert {row["field"] for row in data["prefill"]["left_blank"]} == {"capacity_mw", "stage"}
    assert data["links"][0]["record"]["facts"]["capacity_mw"] == 250.0, "the live value is shown beside it"


def test_export_route_is_handed_to_lane_p4_at_export_user_data(
    browser: Browser, person: UserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = person()
    tab = browser(user)
    resp = tab.get("/v1/my/export")
    assert resp.status_code == 503 and resp.json()["code"] == "unavailable"
    assert resp.headers["cache-control"] == "private, no-store"
    seen: list[str] = []

    def body(db: Session, who: User) -> dict[str, Any]:
        seen.append(who.public_id)
        return {"schema": "inframation.my-export/1"}

    monkeypatch.setattr(my_projects, "export_user_data", body)
    resp = tab.get("/v1/my/export")
    assert resp.status_code == 200 and resp.json() == {"schema": "inframation.my-export/1"}
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert seen == [user.public_id]
    for _ in range(my_projects.EXPORTS_PER_DAY - 2):
        assert tab.get("/v1/my/export").status_code == 200
    assert tab.get("/v1/my/export").status_code == 429


def test_a_cross_site_navigation_to_the_export_is_refused_before_it_costs_anything(
    browser: Browser, person: UserFactory
) -> None:
    """`SameSite=Lax` sends the cookie on another site's top-level navigation, so a link elsewhere
    could spend a user's daily exports (lane P2 security review, 2026-10-23)."""
    tab = browser(person())
    for _ in range(my_projects.EXPORTS_PER_DAY + 1):
        refused = tab.get("/v1/my/export", headers={"Sec-Fetch-Site": "cross-site"})
        assert refused.status_code == 400 and refused.json()["errors"][0]["field"] == "Sec-Fetch-Site"
        assert refused.headers["cache-control"] == "private, no-store"
    for site in ("same-origin", "same-site", "none", None):
        headers = {"Sec-Fetch-Site": site} if site else {}
        assert tab.get("/v1/my/export", headers=headers).status_code == 503, site


# ============================================================================== the one 404
def _comparable(problem: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in problem.items() if k not in ("request_id", "instance")}


def test_outsiders_get_the_404_an_unknown_id_gets(browser: Browser, person: UserFactory) -> None:
    owner_tab, outsider_tab = browser(person()), browser(person())
    project = _create(owner_tab)["public_id"]
    for suffix in ("", "/links", "/checklist", "/notes", "/members", "/invitations", "/activity"):
        real = outsider_tab.get(f"/v1/my/projects/{project}{suffix}")
        missing = outsider_tab.get(f"/v1/my/projects/{MISSING_PROJECT}{suffix}")
        malformed = outsider_tab.get(f"/v1/my/projects/not-an-id{suffix}")
        assert real.status_code == missing.status_code == malformed.status_code == 404, suffix
        assert _comparable(real.json()) == _comparable(missing.json()) == _comparable(malformed.json())
    assert outsider_tab.patch(f"/v1/my/projects/{project}", json={"title": "x"}).status_code == 404
    assert outsider_tab.delete(f"/v1/my/projects/{project}").status_code == 404
    assert outsider_tab.get("/v1/my/projects").json()["data"] == []
    assert outsider_tab.get("/v1/my/pins").json()["data"] == []


def test_a_member_without_the_role_gets_403_forbidden_role(browser: Browser, person: UserFactory) -> None:
    owner, viewer = person(), person("viewer@example.org")
    owner_tab, viewer_tab = browser(owner), browser(viewer)
    project = _create(owner_tab)["public_id"]
    _invite_and_join(owner_tab, project, viewer_tab, "viewer@example.org", "viewer")
    assert viewer_tab.get(f"/v1/my/projects/{project}").status_code == 200
    for method, suffix, body in (
        ("PATCH", "", {"title": "Mine now"}),
        ("PATCH", "", {"archived": True}),
        ("DELETE", "", None),
        ("POST", "/notes", {"body": "hello"}),
        ("POST", "/checklist", {"name": "Thing", "folder": "contracts"}),
        ("GET", "/invitations", None),
        ("POST", "/invitations", {"email": "x@example.org", "role": "viewer"}),
    ):
        resp = viewer_tab.request(method, f"/v1/my/projects/{project}{suffix}", json=body)
        assert resp.status_code == 403, (method, suffix, resp.text)
        assert resp.json()["code"] == "forbidden_role"


def test_an_api_key_never_reaches_a_private_project(
    browser: Browser, person: UserFactory, db: Session
) -> None:
    owner = person()
    project = _create(browser(owner))["public_id"]
    _key, secret = make_api_key(db, owner.account, owner, scopes=["admin:*", "read:live", "read:bulk"])
    db.commit()
    keyed = TestClient(app)
    headers = {"Authorization": f"Bearer {secret}", "Origin": ORIGIN}
    assert keyed.get(f"/v1/my/projects/{project}", headers=headers).status_code == 404
    assert keyed.get("/v1/my/projects", headers=headers).status_code == 401
    assert keyed.get("/v1/my/pins", headers=headers).status_code == 401
    assert keyed.post("/v1/my/projects", json={"title": "x"}, headers=headers).status_code == 401


def test_every_private_route_is_off_until_the_launch_switch_is_on(
    client: TestClient, browser: Browser, person: UserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/19 §8 go-live gate: the API ships before the pages, so a deployment without
    `MY_PIPELINE=on` answers every /v1/my route with the same 404 a missing project gets."""
    monkeypatch.delenv("MY_PIPELINE", raising=False)
    client.headers["Origin"] = ORIGIN
    signed_in = browser(person())
    signed_in.headers["Origin"] = ORIGIN
    for caller in (client, signed_in):
        assert caller.get("/v1/my/projects").status_code == 404
        assert caller.get("/v1/my/pins").status_code == 404
        assert caller.post("/v1/my/projects", json={"title": "x"}).status_code == 404
    monkeypatch.setenv("MY_PIPELINE", "on")
    assert signed_in.get("/v1/my/projects").status_code == 200


def test_signed_out_and_unverified_callers(client: TestClient, browser: Browser, person: UserFactory) -> None:
    client.headers["Origin"] = ORIGIN
    assert client.get("/v1/my/projects").status_code == 401
    assert client.post("/v1/my/projects", json={"title": "x"}).status_code == 401
    assert client.get(f"/v1/my/projects/{MISSING_PROJECT}").status_code == 404
    unverified = browser(person(verified=False))
    resp = unverified.get("/v1/my/projects")
    assert resp.status_code == 403 and resp.json()["title"] == "Verify your email address first"


def test_removed_member_refused_on_next_request(browser: Browser, person: UserFactory) -> None:
    owner, editor = person(), person("ed@example.org")
    owner_tab, editor_tab = browser(owner), browser(editor)
    project = _create(owner_tab)["public_id"]
    _invite_and_join(owner_tab, project, editor_tab, "ed@example.org", "editor")
    assert editor_tab.get(f"/v1/my/projects/{project}").status_code == 200
    assert owner_tab.delete(f"/v1/my/projects/{project}/members/{editor.public_id}").status_code == 204
    assert editor_tab.get(f"/v1/my/projects/{project}").status_code == 404
    assert editor_tab.get("/v1/my/pins").json()["data"] == []


# ===================================================================== headers and the guard
def test_every_private_response_is_no_store_noindex_no_referrer(
    client: TestClient, browser: Browser, person: UserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = person()
    tab = browser(user)
    project = _create(tab)["public_id"]

    def broken(*_args: object) -> set[Any]:
        raise RuntimeError("an unexpected failure inside a private route")

    # A 500 is sent by Starlette's error middleware, outside every user middleware: before
    # 2026-10-23 it went out with none of these headers (lane P2 security review).
    with monkeypatch.context() as patched:
        patched.setattr(my_projects, "_member_ids", broken)
        failed = browser(user, raise_errors=False).get(f"/v1/my/projects/{project}/notes")
    assert failed.status_code == 500 and failed.json()["code"] == "internal_error", failed.text
    assert "unexpected failure" not in failed.text and project not in failed.text.replace(
        f"/v1/my/projects/{project}/notes", ""
    )
    responses = [
        failed,
        tab.get("/v1/my/projects"),
        tab.get(f"/v1/my/projects/{project}"),
        tab.get(f"/v1/my/projects/{MISSING_PROJECT}"),
        tab.post("/v1/my/projects", json={}),
        tab.get("/v1/my/pins?x=1"),
        tab.post(f"/v1/my/projects/{project}/notes", json={"body": "x"}),
        tab.delete(f"/v1/my/projects/{project}"),
        client.get("/v1/my/projects"),
        client.get("/v1/my/no-such-route"),
    ]
    for resp in responses:
        assert resp.headers["cache-control"] == "private, no-store", resp.request.url
        assert resp.headers["x-robots-tag"] == "noindex, nofollow", resp.request.url
        assert resp.headers["referrer-policy"] == "no-referrer", resp.request.url
        assert "cookie" in resp.headers["vary"].lower(), resp.request.url
        assert "etag" not in resp.headers, resp.request.url
    public = client.get("/v1/proposals")
    assert "x-robots-tag" not in public.headers, "the public surface is untouched"


@pytest.mark.parametrize(
    ("path", "private"), [("/v1/proposals", False), ("/v1/my/pins", True), ("/v1/myself", False)]
)
def test_the_500_handler_adds_the_private_headers_on_private_paths_only(path: str, private: bool) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "scheme": "http",
        "server": ("testserver", 80),
    }
    response = asyncio.run(my_projects.private_unhandled_exception_handler(Request(scope), RuntimeError("x")))
    assert response.status_code == 500 and response.headers["x-request-id"]
    for name, value in my_projects.PRIVATE_RESPONSE_HEADERS.items():
        assert (response.headers.get(name) == value) is private, (path, name)
    assert ("cookie" in response.headers.get("vary", "").lower()) is private


def test_cross_site_writes_are_refused(
    browser: Browser, person: UserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tab = browser(person())
    assert (
        tab.post(
            "/v1/my/projects", json={"title": "x"}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 400
    )
    del tab.headers["Origin"]
    resp = tab.post("/v1/my/projects", json={"title": "x"})
    assert resp.status_code == 400 and resp.json()["errors"][0]["field"] == "Origin"
    tab.headers["Origin"] = ORIGIN
    form = tab.post(
        "/v1/my/projects", content=b"title=x", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert form.status_code == 400 and form.json()["errors"][0]["field"] == "Content-Type"
    assert tab.post("/v1/my/projects?title=x", json={"title": "x"}).status_code == 400
    assert tab.get("/v1/my/projects").status_code == 200, "reads need no Origin check"
    # The site's own server-side call has done its own same-origin check on the form.
    monkeypatch.setenv("API_INTERNAL_TOKEN", "site-token-for-tests")
    del tab.headers["Origin"]
    assert (
        tab.post(
            "/v1/my/projects", json={"title": "x"}, headers={"X-Internal-Token": "site-token-for-tests"}
        ).status_code
        == 201
    )
    assert tab.post("/v1/my/projects", json={"title": "x"}).status_code == 400
    # Over TLS, a page on this host served over plain http is not this site.
    secure = browser(person(), base_url="https://testserver")
    assert secure.post("/v1/my/projects", json={"title": "x"}).status_code == 201
    insecure = secure.post("/v1/my/projects", json={"title": "x"}, headers={"Origin": "http://testserver"})
    assert insecure.status_code == 400 and insecure.json()["errors"][0]["field"] == "Origin"
    assert (
        tab.post(
            "/v1/my/projects", json={"title": "x"}, headers={"Origin": "https://inframation.io"}
        ).status_code
        == 201
    )


def test_no_private_response_is_stored_for_replay(browser: Browser, person: UserFactory, db: Session) -> None:
    """`idempotency_record` is not private, outlives the account and would hold the invitation link."""
    tab = browser(person())
    first = tab.post("/v1/my/projects", json={"title": "Keyed"}, headers={"Idempotency-Key": "key-0000001"})
    project = first.json()["data"]["public_id"]
    created = tab.post(
        f"/v1/my/projects/{project}/invitations",
        json={"email": "guest@example.org", "role": "viewer"},
        headers={"Idempotency-Key": "key-0000002"},
    )
    assert created.status_code == 201 and "idempotent-replayed" not in created.headers
    again = tab.post(
        f"/v1/my/projects/{project}/invitations",
        json={"email": "guest@example.org", "role": "viewer"},
        headers={"Idempotency-Key": "key-0000002"},
    )
    assert again.status_code == 201 and "idempotent-replayed" not in again.headers
    assert again.json()["data"]["link"] != created.json()["data"]["link"], "a retry replaces, never replays"
    assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


# ================================================================================ invitations
def test_invitation_link_is_shown_once_and_accept_joins(
    browser: Browser, person: UserFactory, db: Session
) -> None:
    owner, guest = person(name="Olu Owner"), person("guest@example.org")
    owner_tab, guest_tab = browser(owner), browser(guest)
    project = _create(owner_tab, title="Shared Deal")["public_id"]
    created = owner_tab.post(
        f"/v1/my/projects/{project}/invitations", json={"email": "Guest@Example.org", "role": "editor"}
    )
    link = created.json()["data"]["link"]
    token = link.split("t=", 1)[1]
    assert link.startswith("https://inframation.io/my/invitations/accept?t=")
    listed = owner_tab.get(f"/v1/my/projects/{project}/invitations")
    assert token not in listed.text and "hash" not in listed.text
    assert set(listed.json()["data"][0]) == {"invitation_id", "email", "role", "created_at", "expires_at"}
    stored = db.scalar(select(pm.PrivateProjectInvitation))
    assert token not in json.dumps({c.name: str(getattr(stored, c.name)) for c in stored.__table__.columns})
    preview = guest_tab.post("/v1/my/invitations/preview", json={"token": token})
    assert preview.json()["data"] == {
        "project": {"title": "Shared Deal"},
        "role": "editor",
        "inviter_name": "Olu Owner",
        "expires_at": preview.json()["data"]["expires_at"],
    }
    accepted = guest_tab.post("/v1/my/invitations/accept", json={"token": token})
    assert accepted.json()["data"]["project"]["public_id"] == project
    assert accepted.json()["data"]["role"] == "editor"
    assert guest_tab.post("/v1/my/invitations/accept", json={"token": token}).status_code == 404
    assert guest_tab.get(f"/v1/my/projects/{project}").json()["data"]["role"] == "editor"


def test_invitation_failures_are_one_404_and_revocation_bites(browser: Browser, person: UserFactory) -> None:
    owner, guest, stranger = person(), person("guest@example.org"), person("stranger@example.org")
    owner_tab, guest_tab, stranger_tab = browser(owner), browser(guest), browser(stranger)
    project = _create(owner_tab)["public_id"]
    created = owner_tab.post(
        f"/v1/my/projects/{project}/invitations", json={"email": "guest@example.org", "role": "viewer"}
    )
    token = created.json()["data"]["link"].split("t=", 1)[1]
    wrong_address = stranger_tab.post("/v1/my/invitations/accept", json={"token": token})
    wrong_token = guest_tab.post("/v1/my/invitations/accept", json={"token": "x" * 43})
    assert wrong_address.status_code == wrong_token.status_code == 404
    assert _comparable(wrong_address.json()) == _comparable(wrong_token.json())
    invitation = created.json()["data"]["invitation_id"]
    assert owner_tab.delete(f"/v1/my/projects/{project}/invitations/{invitation}").status_code == 204
    revoked = guest_tab.post("/v1/my/invitations/accept", json={"token": token})
    assert revoked.status_code == 404 and _comparable(revoked.json()) == _comparable(wrong_token.json())
    assert owner_tab.delete(f"/v1/my/projects/{project}/invitations/{invitation}").status_code == 404
    assert owner_tab.delete(f"/v1/my/projects/{project}/invitations/not-an-id").status_code == 404


def test_invitation_creation_is_rate_limited_per_user(browser: Browser, person: UserFactory) -> None:
    owner_tab = browser(person())
    projects = [_create(owner_tab, title=f"Deal {n}")["public_id"] for n in range(2)]
    for n in range(my_projects.INVITATIONS_PER_HOUR):
        project = projects[n % 2]
        resp = owner_tab.post(
            f"/v1/my/projects/{project}/invitations", json={"email": f"g{n}@example.org", "role": "viewer"}
        )
        assert resp.status_code == 201, resp.text
    refused = owner_tab.post(
        f"/v1/my/projects/{projects[0]}/invitations", json={"email": "late@example.org", "role": "viewer"}
    )
    assert refused.status_code == 429 and refused.headers["retry-after"]


# ======================================================================================= notes
def test_notes_newest_first_author_edits_owner_deletes(browser: Browser, person: UserFactory) -> None:
    owner, editor = person(name="Olu Owner"), person("ed@example.org", name="Eda Editor")
    owner_tab, editor_tab = browser(owner), browser(editor)
    project = _create(owner_tab)["public_id"]
    _invite_and_join(owner_tab, project, editor_tab, "ed@example.org", "editor")
    first = owner_tab.post(f"/v1/my/projects/{project}/notes", json={"body": "First\nline two"}).json()[
        "data"
    ]
    second = editor_tab.post(f"/v1/my/projects/{project}/notes", json={"body": "Second"}).json()["data"]
    listed = owner_tab.get(f"/v1/my/projects/{project}/notes").json()["data"]
    assert [n["note_id"] for n in listed] == [second["note_id"], first["note_id"]]
    assert listed[0]["author"] == {
        "user_public_id": editor.public_id,
        "name": "Eda Editor",
        "former_member": False,
    }
    assert (
        owner_tab.patch(
            f"/v1/my/projects/{project}/notes/{second['note_id']}", json={"body": "no"}
        ).status_code
        == 403
    )
    edited = editor_tab.patch(
        f"/v1/my/projects/{project}/notes/{second['note_id']}", json={"body": "Second, edited"}
    )
    assert edited.json()["data"]["body"] == "Second, edited" and edited.json()["data"]["edited"] is True
    assert editor_tab.delete(f"/v1/my/projects/{project}/notes/{first['note_id']}").status_code == 403
    assert owner_tab.delete(f"/v1/my/projects/{project}/notes/{second['note_id']}").status_code == 204
    assert owner_tab.post(f"/v1/my/projects/{project}/notes", json={"body": "bad\x00"}).status_code == 400
    assert owner_tab.post(f"/v1/my/projects/{project}/notes", json={"body": "x" * 20_001}).status_code == 400
    assert (
        owner_tab.patch(f"/v1/my/projects/{project}/notes/nte_0000000000", json={"body": "x"}).status_code
        == 404
    )


def test_a_departed_authors_notes_show_former_member(browser: Browser, person: UserFactory) -> None:
    owner, editor = person(), person("ed@example.org", name="Eda Editor")
    owner_tab, editor_tab = browser(owner), browser(editor)
    project = _create(owner_tab)["public_id"]
    _invite_and_join(owner_tab, project, editor_tab, "ed@example.org", "editor")
    editor_tab.post(f"/v1/my/projects/{project}/notes", json={"body": "Kept"})
    assert editor_tab.delete(f"/v1/my/projects/{project}/members/{editor.public_id}").status_code == 204
    note = owner_tab.get(f"/v1/my/projects/{project}/notes").json()["data"][0]
    assert note["body"] == "Kept"
    assert note["author"] == {"user_public_id": None, "name": "Former member", "former_member": True}
    activity = owner_tab.get(f"/v1/my/projects/{project}/activity")
    assert "Eda Editor" not in activity.text


# =================================================================================== checklist
def test_checklist_statuses_have_no_verified_and_count_by_pillar(
    browser: Browser, person: UserFactory
) -> None:
    tab = browser(person())
    project = _create(tab)["public_id"]
    checklist = tab.get(f"/v1/my/projects/{project}/checklist").json()["data"]
    assert [f["folder"] for f in checklist["folders"]] == list(pm.CHECKLIST_FOLDERS)
    ppa = next(i for f in checklist["folders"] for i in f["items"] if i["template_key"] == "ppa")
    assert (
        tab.patch(
            f"/v1/my/projects/{project}/checklist/{ppa['item_id']}", json={"status": "verified"}
        ).status_code
        == 400
    )
    done = tab.patch(f"/v1/my/projects/{project}/checklist/{ppa['item_id']}", json={"status": "done"}).json()[
        "data"
    ]
    assert done["status"] == "done" and done["status_label"] == "Done (your status)"
    readiness = {
        row["pillar"]: row for row in tab.get(f"/v1/my/projects/{project}").json()["data"]["readiness"]
    }
    assert list(readiness) == ["completeness", "fundamentals", "counterparties", "regulatory_site", "sponsor"]
    assert (readiness["completeness"]["done"], readiness["completeness"]["total"]) == (1, 32)
    assert (
        readiness["counterparties"]["text"]
        == "1 of 5 items done (your status) · 0 with suggested public-record evidence"
    )
    assert "score" not in json.dumps(readiness)
    custom = tab.post(
        f"/v1/my/projects/{project}/checklist", json={"name": "Water rights", "folder": "permits"}
    )
    assert custom.status_code == 201 and custom.json()["data"]["custom"] is True
    assert custom.json()["data"]["pillar"] == "regulatory_site"
    assert (
        tab.patch(f"/v1/my/projects/{project}/checklist/not-a-uuid", json={"status": "done"}).status_code
        == 404
    )


def _interconnection_item(checklist: dict[str, Any]) -> dict[str, Any]:
    return next(
        i
        for f in checklist["folders"]
        for i in f["items"]
        if i["template_key"] == "interconnection_agreement"
    )


def test_suggestions_are_labelled_never_verified_and_decisions_keep_status(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    tab = browser(person())
    proposal = world["proposal"]
    project = _create(tab, **{"from": {"record_type": "proposal", "public_id": proposal.public_id}})[
        "public_id"
    ]
    checklist = tab.get(f"/v1/my/projects/{project}/checklist").json()["data"]
    item = _interconnection_item(checklist)
    (suggestion,) = item["suggestions"]
    assert suggestion["label"] == "Suggested from public records · not verified"
    assert suggestion["rule"] == "queue_position" and suggestion["state"] == "suggested"
    assert (
        suggestion["text"]
        == "Test ISO queue Q1, status as stated: filed. A queue record, not an interconnection agreement."
    )
    assert suggestion["source"]["source_id"] == world["source"].id and suggestion["retrieved_at"]
    assert "verified" not in json.dumps(checklist).replace("not verified", "").replace(
        "Nothing here is verified", ""
    )
    base = f"/v1/my/projects/{project}/checklist/{item['item_id']}/evidence/{suggestion['evidence_id']}"
    accepted = tab.post(f"{base}/accept")
    assert accepted.status_code == 200 and accepted.json()["data"]["state"] == "accepted"
    item = _interconnection_item(tab.get(f"/v1/my/projects/{project}/checklist").json()["data"])
    assert item["status"] == "not_started" and item["suggestions"][0]["state"] == "accepted"
    assert tab.post(f"{base}/dismiss").json()["data"]["state"] == "dismissed"
    item = _interconnection_item(tab.get(f"/v1/my/projects/{project}/checklist").json()["data"])
    assert item["suggestions"] == [] and item["status"] == "not_started"
    readiness = tab.get(f"/v1/my/projects/{project}").json()["data"]["readiness"]
    assert readiness[0]["with_suggested_evidence"] == 0
    # Rebuilt live: the record's status changes, so does the text; hidden, it is gone.
    proposal.lifecycle_state = "withdrawn"
    db.commit()
    assert tab.post(f"{base}/accept").status_code == 200
    item = _interconnection_item(tab.get(f"/v1/my/projects/{project}/checklist").json()["data"])
    assert item["suggestions"] == [], "a withdrawn record no longer fires the queue rule"


def _proposal_on(db: Session, source: Source, suffix: str, *, ia_date: str | None = None) -> Proposal:
    proposal = make_visible_proposal(db, source, public_id_suffix=suffix)
    link = proposal.sources[0]
    link.normalised = {"ia_date": ia_date} if ia_date else {}
    db.commit()
    return proposal


def test_each_evidence_rule_fires_only_from_its_own_sources(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    licence = world["licence"]
    tec = make_public_source(db, licence, id_=my_projects.TEC_REGISTER_SOURCE)
    tec.operator = "NESO"
    air = make_public_source(db, licence, id_="us.tx.tceq.air_permits")
    air.category, air.operator = "permit", "TCEQ"
    federal = make_public_source(db, licence, id_="us.blm.eplanning")
    federal.category, federal.operator = "permit", "BLM"
    other_permit = make_public_source(db, licence, id_="us.state.siting_boards")
    other_permit.category = "permit"
    db.commit()
    tab = browser(person())
    project = _create(tab)["public_id"]
    records = {
        "ia": _proposal_on(db, world["source"], "11", ia_date="2026-03-02"),
        "tec": _proposal_on(db, tec, "12"),
        "air": _proposal_on(db, air, "13"),
        "federal": _proposal_on(db, federal, "14"),
        "other": _proposal_on(db, other_permit, "15"),
    }
    for record in records.values():
        tab.post(
            f"/v1/my/projects/{project}/links",
            json={"record_type": "proposal", "public_id": record.public_id, "relation": "comparable"},
        )
    checklist = tab.get(f"/v1/my/projects/{project}/checklist").json()["data"]
    fired = {
        (s["record"]["public_id"], s["rule"], i["template_key"])
        for f in checklist["folders"]
        for i in f["items"]
        for s in i["suggestions"]
    }
    assert fired == {
        (records["ia"].public_id, "queue_position", "interconnection_agreement"),
        (records["ia"].public_id, "executed_interconnection_agreement", "interconnection_agreement"),
        (records["tec"].public_id, "gb_tec_register", "grid_connection_agreement"),
        (records["air"].public_id, "air_permit", "air_quality_permit"),
        (records["federal"].public_id, "federal_record", "environmental_approvals"),
    }
    texts = {s["rule"]: s["text"] for f in checklist["folders"] for i in f["items"] for s in i["suggestions"]}
    assert texts["executed_interconnection_agreement"].startswith(
        "Test Public Source reports an executed interconnection agreement (as of 2026-03-02)."
    )
    assert texts["gb_tec_register"].startswith("NESO TEC register lists a connection agreement: 112 MW at ")
    assert texts["air_permit"].startswith("Air permit Q13, TCEQ, ")
    assert texts["federal_record"] == "BLM record Q14, status filed."
    never = {
        "ppa",
        "epc_contract",
        "land_rights",
        "land_use_permit",
        "construction_permits",
        "loan_agreement",
    }
    assert not any(
        i["suggestions"] for f in checklist["folders"] for i in f["items"] if i["template_key"] in never
    )
    # A record the tier can no longer read fires nothing.
    air.publish_state = "ingest_only"
    db.commit()
    checklist = tab.get(f"/v1/my/projects/{project}/checklist").json()["data"]
    assert not any(
        s["rule"] == "air_permit" for f in checklist["folders"] for i in f["items"] for s in i["suggestions"]
    )


# ===================================================================================== members
def test_transfer_ownership_and_leave(browser: Browser, person: UserFactory) -> None:
    owner, editor = person(), person("ed@example.org")
    owner_tab, editor_tab = browser(owner), browser(editor)
    project = _create(owner_tab)["public_id"]
    _invite_and_join(owner_tab, project, editor_tab, "ed@example.org", "editor")
    left = owner_tab.delete(f"/v1/my/projects/{project}/members/{owner.public_id}")
    assert left.status_code == 403 and left.json()["code"] == "forbidden_role"
    viewer = owner_tab.patch(f"/v1/my/projects/{project}/members/{editor.public_id}", json={"role": "viewer"})
    assert {m["user_public_id"]: m["role"] for m in viewer.json()["data"]}[editor.public_id] == "viewer"
    moved = owner_tab.patch(f"/v1/my/projects/{project}/members/{editor.public_id}", json={"role": "owner"})
    roles = {m["user_public_id"]: m["role"] for m in moved.json()["data"]}
    assert roles == {editor.public_id: "owner", owner.public_id: "editor"}
    assert owner_tab.delete(f"/v1/my/projects/{project}/members/{owner.public_id}").status_code == 204
    assert owner_tab.get(f"/v1/my/projects/{project}").status_code == 404
    members = editor_tab.get(f"/v1/my/projects/{project}/members").json()["data"]
    assert [m["is_you"] for m in members] == [True] and "email" not in json.dumps(members)
    assert (
        editor_tab.patch(f"/v1/my/projects/{project}/members/usr_bad", json={"role": "viewer"}).status_code
        == 404
    )


# ======================================================================================== pins
def test_pins_carry_exactly_the_documented_fields_and_count_unplaced(
    browser: Browser, person: UserFactory, world: dict[str, Any]
) -> None:
    user, other = person(), person()
    tab, other_tab = browser(user), browser(other)
    placed = _create(tab, title="Placed", lat=30.1, lon=-97.2)
    _create(tab, title="Nowhere")
    archived = _create(tab, title="Old", lat=31.0, lon=-98.0)
    tab.patch(f"/v1/my/projects/{archived['public_id']}", json={"archived": True})
    _create(other_tab, title="Not yours", lat=29.0, lon=-95.0)
    resp = tab.get("/v1/my/pins")
    (pin,) = resp.json()["data"]
    assert set(pin) == {"public_id", "title", "stage", "lat", "lon", "precision", "progress", "role"}
    assert (pin["public_id"], pin["lat"], pin["lon"], pin["precision"], pin["role"]) == (
        placed["public_id"],
        30.1,
        -97.2,
        "user",
        "owner",
    )
    assert pin["progress"] == {"done": 0, "total": 32, "label": "0 of 32 done, your status"}
    assert resp.json()["meta"]["unplaced_count"] == 1
    assert "Not yours" not in resp.text and "Old" not in resp.text


# ===================================================================================== prefill
def test_starting_from_each_record_type(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    tab = browser(person())
    from_proposal = _create(
        tab, title=None, **{"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}}
    )
    assert (from_proposal["title"], from_proposal["technology"], from_proposal["stage"]) == (
        world["proposal"].name_canonical,
        "solar",
        "development",
    )
    assert (from_proposal["lat"], from_proposal["lon"], from_proposal["point_precision"]) == (
        30.27,
        -97.74,
        "exact",
    )
    assert from_proposal["origin"] == {"record_type": "proposal", "public_id": world["proposal"].public_id}
    assert from_proposal["links"][0]["relation"] == "this_project"
    nearby = _create(
        tab,
        **{
            "from": {
                "record_type": "proposal",
                "public_id": world["proposal"].public_id,
                "relation": "nearby",
            }
        },
    )
    assert nearby["stage"] == "prospecting" and "stage" not in nearby["prefill"]["fields"]
    plant = _create(tab, **{"from": {"record_type": "asset", "public_id": world["plant"].public_id}})
    assert plant["title"] == "North Ridge Solar", "a field the body gives is the user's own"
    assert plant["point_precision"] == "exact" and plant["links"][0]["relation"] == "grid_connection"
    preview = tab.post("/v1/my/prefill", json={"record_type": "asset", "public_id": world["plant"].public_id})
    assert preview.json()["data"]["values"]["title"] == "New project at Old Coal Plant"
    rfp = tab.post(
        "/v1/my/prefill", json={"record_type": "opportunity", "public_id": world["opportunity"].public_id}
    )
    assert rfp.json()["data"]["values"] == {"title": "Bid: Test RFP 1", "technology": "solar"}
    assert rfp.json()["data"]["relation"] == "offtake_or_rfp"
    point = InterconnectionPoint(
        public_id="",
        operator="ERCOT",
        name_display="Bearkat 345kV",
        name_key="sub:bearkat|345",
        key_rule="test",
        kind="substation",
        voltage_kv=345,
        source_id=world["source"].id,
        source_url=world["source"].url,
        retrieved_at=dt.datetime(2026, 10, 1, tzinfo=UTC),
        licence_id=world["licence"].id,
    )
    db.add(point)
    db.flush()
    point.public_id = public_id("poi", point.id)
    world["proposal"].interconnection_point_id = point.id
    db.commit()
    poi = tab.post(
        "/v1/my/prefill", json={"record_type": "interconnection_point", "public_id": point.public_id}
    )
    assert poi.json()["data"]["values"] == {"title": "Project at Bearkat 345kV (345 kV)"}


def test_start_a_deal_is_offered_only_where_docs_19_offers_it(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    tab = browser(person())
    operating = make_asset(db, world["source"], world["licence"], source_asset_id="2", name="Running Plant")
    withdrawn = make_visible_proposal(db, world["source"], public_id_suffix="9", lifecycle_state="withdrawn")
    db.commit()
    for record_type, record_id, relation in (
        ("proposal", withdrawn.public_id, None),
        ("asset", operating.public_id, None),
        ("proposal", world["proposal"].public_id, "funding"),
        ("proposal", "prop_0000000000", None),
        ("organization", "org_0000000000", None),
        ("corridor", "c1", None),
    ):
        body: dict[str, Any] = {"record_type": record_type, "public_id": record_id}
        if relation:
            body["relation"] = relation
        resp = tab.post("/v1/my/prefill", json=body)
        assert resp.status_code == 400, (record_type, relation)
    assert tab.get("/v1/my/projects").json()["data"] == []


# ======================================================================================= links
def test_links_are_idempotent_capped_and_corridors_wait_for_p6(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tab = browser(person())
    project = _create(tab, **{"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}})[
        "public_id"
    ]
    body = {"record_type": "proposal", "public_id": world["proposal"].public_id, "relation": "this_project"}
    again = tab.post(f"/v1/my/projects/{project}/links", json=body)
    assert again.status_code == 200
    corridor = tab.post(
        f"/v1/my/projects/{project}/links",
        json={"record_type": "corridor", "public_id": "c1", "relation": "candidate_site"},
    )
    assert corridor.status_code == 400 and "site finder" in corridor.json()["detail"]
    tab.get(f"/v1/my/projects/{project}/checklist")
    access_project = db.scalar(select(pm.PrivateProject).where(pm.PrivateProject.public_id == project))
    count_evidence = (
        select(func.count())
        .select_from(pm.PrivateProjectEvidence)
        .where(pm.PrivateProjectEvidence.project_id == access_project.id)
    )
    assert db.scalar(count_evidence) == 1
    link_id = again.json()["data"]["link_id"]
    assert tab.delete(f"/v1/my/projects/{project}/links/{link_id}").status_code == 204
    assert db.scalar(count_evidence) == 0, "evidence goes with the last link to its record"
    assert tab.delete(f"/v1/my/projects/{project}/links/{link_id}").status_code == 404
    monkeypatch.setattr(pm, "MAX_LINKS_PER_PROJECT", 1)
    tab.post(f"/v1/my/projects/{project}/links", json=body)
    capped = tab.post(f"/v1/my/projects/{project}/links", json={**body, "relation": "nearby"})
    assert capped.status_code == 409


# ============================================================================ concurrent requests
# Each test stages, in one request, the moment a concurrent request's row lands. Before 2026-10-23,
# 8 parallel requests against a live server on SQLite and on Postgres gave 500s (IntegrityError on
# the unique link and evidence constraints): 15 of 40 checklist views and 21 of 40 link adds on
# SQLite, 19 and 29 of 40 on Postgres (lane P2 security review).
def _project_row(db: Session, public_id: str) -> pm.PrivateProject:
    db.expire_all()
    row = db.scalar(select(pm.PrivateProject).where(pm.PrivateProject.public_id == public_id))
    assert row is not None
    return row


def _store_competing_link(session: Session, project: pm.PrivateProject, body: dict[str, str]) -> uuid.UUID:
    link_id = uuid.uuid4()
    session.connection().execute(
        insert(pm.PrivateProjectLink.__table__).values(
            id=link_id,
            project_id=project.id,
            record_type=body["record_type"],
            record_public_id=body["public_id"],
            relation=body["relation"],
            created_at=dt.datetime.now(UTC),
        )
    )
    return link_id


@pytest.mark.parametrize("landed", ["while_waiting_for_the_lock", "after_the_last_check_without_a_row_lock"])
def test_a_concurrent_add_of_the_same_link_answers_with_the_stored_one(
    landed: str,
    browser: Browser,
    person: UserFactory,
    db: Session,
    db_sessionmaker: sessionmaker[Session],
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres: the other add commits while this one waits for `lock_project`. SQLite takes no row
    lock, so the other add can land after this one's last look. Either way, one link and a 200."""
    tab = browser(person(), raise_errors=False)
    project = _create(tab)["public_id"]
    row = _project_row(db, project)
    body = {"record_type": "opportunity", "public_id": world["opportunity"].public_id, "relation": "funding"}
    stored: list[uuid.UUID] = []
    if landed == "while_waiting_for_the_lock":
        real_lock = authz.lock_project

        def lock_granted_after_the_other_add(session: Session, project_row: pm.PrivateProject) -> None:
            real_lock(session, project_row)
            if not stored:
                stored.append(_store_competing_link(session, project_row, body))

        monkeypatch.setattr(authz, "lock_project", lock_granted_after_the_other_add)
        resp = tab.post(f"/v1/my/projects/{project}/links", json=body)
    else:

        def lands_after_the_count(state: ORMExecuteState) -> Any:
            sql = str(state.statement)
            if stored or not state.is_select or "count(" not in sql or "private_project_link" not in sql:
                return None
            result = state.invoke_statement().freeze()
            stored.append(_store_competing_link(state.session, row, body))
            return result()

        event.listen(db_sessionmaker, "do_orm_execute", lands_after_the_count)
        try:
            resp = tab.post(f"/v1/my/projects/{project}/links", json=body)
        finally:
            event.remove(db_sessionmaker, "do_orm_execute", lands_after_the_count)
    assert stored, "the race was staged"
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["link_id"] == str(stored[0])
    assert resp.headers["cache-control"] == "private, no-store"
    db.expire_all()
    links = db.scalars(select(pm.PrivateProjectLink).where(pm.PrivateProjectLink.project_id == row.id)).all()
    assert [link.id for link in links] == stored


def _queue_item(db: Session, project: pm.PrivateProject) -> pm.PrivateProjectItem:
    item = db.scalar(
        select(pm.PrivateProjectItem).where(
            pm.PrivateProjectItem.project_id == project.id,
            pm.PrivateProjectItem.template_key == "interconnection_agreement",
        )
    )
    assert item is not None
    return item


def _store_competing_suggestion(
    session: Session, project: pm.PrivateProject, item: pm.PrivateProjectItem, record_public_id: str
) -> uuid.UUID:
    evidence_id = uuid.uuid4()
    session.connection().execute(
        insert(pm.PrivateProjectEvidence.__table__).values(
            id=evidence_id,
            project_id=project.id,
            item_id=item.id,
            record_type="proposal",
            record_public_id=record_public_id,
            rule="queue_position",
            state="suggested",
            created_at=dt.datetime.now(UTC),
        )
    )
    return evidence_id


def _evidence_ids(db: Session, project: pm.PrivateProject) -> list[str]:
    db.expire_all()
    rows = db.scalars(
        select(pm.PrivateProjectEvidence).where(pm.PrivateProjectEvidence.project_id == project.id)
    ).all()
    return [str(row.id) for row in rows]


def test_concurrent_first_views_of_a_suggestion_wait_and_share_one_row(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two members open the checklist just after a link. The view that has a new suggestion to
    store holds the project first; the other view's row, stored while it waited, is the one it shows."""
    tab = browser(person(), raise_errors=False)
    proposal = world["proposal"].public_id
    project = _create(tab, **{"from": {"record_type": "proposal", "public_id": proposal}})["public_id"]
    row = _project_row(db, project)
    item = _queue_item(db, row)
    stored: list[uuid.UUID] = []
    real_lock = authz.lock_project

    def lock_granted_after_the_other_view(session: Session, project_row: pm.PrivateProject) -> None:
        real_lock(session, project_row)
        if not stored:
            stored.append(_store_competing_suggestion(session, project_row, item, proposal))

    with monkeypatch.context() as patched:
        patched.setattr(authz, "lock_project", lock_granted_after_the_other_view)
        resp = tab.get(f"/v1/my/projects/{project}/checklist")
        assert stored, "the view held the project before storing its suggestion"
        assert resp.status_code == 200, resp.text
        (suggestion,) = _interconnection_item(resp.json()["data"])["suggestions"]
        assert suggestion["evidence_id"] == str(stored[0])
        assert _evidence_ids(db, row) == [str(stored[0])]
        held: list[bool] = []
        patched.setattr(authz, "lock_project", lambda *_args: held.append(True))
        again = tab.get(f"/v1/my/projects/{project}/checklist")
        assert again.status_code == 200 and not held, "a view with nothing new to store takes no hold"


def test_a_suggestion_stored_by_a_writer_that_skips_the_hold_is_not_a_500(
    browser: Browser,
    person: UserFactory,
    db: Session,
    db_sessionmaker: sessionmaker[Session],
    world: dict[str, Any],
) -> None:
    """The savepoint behind the hold: the other row lands between this view's check and its
    insert, and the view still answers 200 with one stored row."""
    tab = browser(person(), raise_errors=False)
    proposal = world["proposal"].public_id
    project = _create(tab, **{"from": {"record_type": "proposal", "public_id": proposal}})["public_id"]
    row = _project_row(db, project)
    item = _queue_item(db, row)
    staged: list[uuid.UUID] = []

    def lands_before_the_insert(session: Session, _context: object, _instances: object) -> None:
        if staged or not any(isinstance(obj, pm.PrivateProjectEvidence) for obj in session.new):
            return
        staged.append(_store_competing_suggestion(session, row, item, proposal))

    event.listen(db_sessionmaker, "before_flush", lands_before_the_insert)
    try:
        resp = tab.get(f"/v1/my/projects/{project}/checklist")
    finally:
        event.remove(db_sessionmaker, "before_flush", lands_before_the_insert)
    assert staged, "the race was staged"
    assert resp.status_code == 200, resp.text
    (suggestion,) = _interconnection_item(resp.json()["data"])["suggestions"]
    assert _evidence_ids(db, row) == [suggestion["evidence_id"]]


# ============================================================================ archive, delete
def test_archive_is_owner_only_and_freezes_edits(browser: Browser, person: UserFactory) -> None:
    owner, editor = person(), person("ed@example.org")
    owner_tab, editor_tab = browser(owner), browser(editor)
    project = _create(owner_tab)["public_id"]
    _invite_and_join(owner_tab, project, editor_tab, "ed@example.org", "editor")
    assert editor_tab.patch(f"/v1/my/projects/{project}", json={"archived": True}).status_code == 403
    archived = owner_tab.patch(f"/v1/my/projects/{project}", json={"archived": True})
    assert archived.json()["data"]["archived"] is True
    assert owner_tab.patch(f"/v1/my/projects/{project}", json={"title": "Edited"}).status_code == 404
    assert editor_tab.post(f"/v1/my/projects/{project}/notes", json={"body": "x"}).status_code == 404
    assert owner_tab.get(f"/v1/my/projects/{project}").status_code == 200
    assert owner_tab.get("/v1/my/projects").json()["data"] == []
    assert [
        p["public_id"] for p in owner_tab.get("/v1/my/projects?include_archived=true").json()["data"]
    ] == [project]
    both = owner_tab.patch(f"/v1/my/projects/{project}", json={"archived": False, "title": "Back"})
    assert both.json()["data"]["archived"] is False and both.json()["data"]["title"] == "Back"
    assert owner_tab.delete(f"/v1/my/projects/{project}").status_code == 204
    assert owner_tab.get(f"/v1/my/projects/{project}").status_code == 404


def test_private_operations_write_no_event_or_ui_event(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    before = db.scalar(select(func.count()).select_from(Event))
    tab = browser(person())
    project = _create(tab, **{"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}})[
        "public_id"
    ]
    tab.post(f"/v1/my/projects/{project}/notes", json={"body": "x"})
    tab.get(f"/v1/my/projects/{project}/checklist")
    tab.post(f"/v1/my/projects/{project}/invitations", json={"email": "g@example.org", "role": "viewer"})
    tab.patch(f"/v1/my/projects/{project}", json={"archived": True})
    tab.delete(f"/v1/my/projects/{project}")
    assert db.scalar(select(func.count()).select_from(Event)) == before


def test_activity_names_ids_and_never_values(
    browser: Browser, person: UserFactory, world: dict[str, Any]
) -> None:
    tab = browser(person())
    project = _create(
        tab,
        title="Secret Valley",
        **{"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}},
    )["public_id"]
    tab.post(f"/v1/my/projects/{project}/notes", json={"body": "the strike price is 41"})
    tab.patch(f"/v1/my/projects/{project}", json={"title": "Secret Valley II"})
    resp = tab.get(f"/v1/my/projects/{project}/activity?limit=10")
    actions = [row["action"] for row in resp.json()["data"]]
    assert actions[:3] == ["field_changed", "note_added", "link_added"]
    for value in ("Secret Valley", "strike price", world["proposal"].name_canonical):
        assert value not in resp.text
    assert tab.get(f"/v1/my/projects/{project}/activity?limit=0").status_code == 400
    assert tab.get(f"/v1/my/projects/{project}/activity?since=1").status_code == 400


def test_activity_limit_takes_ascii_digits_only(browser: Browser, person: UserFactory) -> None:
    """`"²".isdigit()` is true and `int("²")` raises: that was a 500 (lane P2 security review)."""
    tab = browser(person(), raise_errors=False)
    project = _create(tab)["public_id"]
    # superscript two, Arabic-Indic three, fullwidth five, exponent, signs, space, too many digits
    for raw in ("%C2%B2", "%D9%A3", "%EF%BC%95", "1e2", "-1", "+5", "%205", "0", "201", "00000"):
        resp = tab.get(f"/v1/my/projects/{project}/activity?limit={raw}")
        assert resp.status_code == 400, (raw, resp.status_code)
        assert resp.json()["errors"][0]["field"] == "limit"
        assert resp.headers["cache-control"] == "private, no-store"
    assert tab.get(f"/v1/my/projects/{project}/activity?limit=0005").status_code == 200


def test_a_record_the_viewer_cannot_see_shows_its_type_and_no_id(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    """A paid member starts a deal from a record still in the free tier's delay and shares it with
    a free member. The free member's link, origin and history say "a proposal" and nothing else:
    the id told them the record exists, and a uuid7 id says when it was made (docs/19 §2.2)."""
    account = make_account(db, entitlement="pro", name="Paid Account")
    paid = make_user(db, account, email="paid@example.org")
    paid.name, paid.email_verified_at = "Paid Member", dt.datetime(2026, 10, 1, tzinfo=UTC)
    delayed = make_visible_proposal(
        db, world["source"], public_id_suffix="9", public_at=dt.datetime.now(UTC) + dt.timedelta(days=7)
    )
    db.commit()
    owner_tab, free_tab = browser(paid), browser(person("free@example.org"))
    project = _create(owner_tab, **{"from": {"record_type": "proposal", "public_id": delayed.public_id}})[
        "public_id"
    ]
    _invite_and_join(owner_tab, project, free_tab, "free@example.org", "viewer")
    detail = free_tab.get(f"/v1/my/projects/{project}").json()["data"]
    assert detail["origin"] == {"record_type": "proposal", "public_id": None}
    (link,) = detail["links"]
    assert (link["record_type"], link["public_id"], link["state"]) == ("proposal", None, "unavailable")
    history = free_tab.get(f"/v1/my/projects/{project}/activity").json()["data"]
    created = next(row for row in history if row["action"] == "created")
    assert created["detail"] == {"kind": "project", "origin_type": "proposal", "origin_public_id": None}
    added = next(row for row in history if row["action"] == "link_added")
    assert added["detail"]["record_type"] == "proposal" and added["detail"]["record_public_id"] is None
    for path in ("", "/links", "/checklist", "/activity"):
        assert delayed.public_id not in free_tab.get(f"/v1/my/projects/{project}{path}").text, path
    # The paid member sees the record, and the stored references were never rewritten.
    mine = owner_tab.get(f"/v1/my/projects/{project}").json()["data"]
    assert mine["origin"]["public_id"] == delayed.public_id
    assert mine["links"][0]["public_id"] == delayed.public_id
    history = owner_tab.get(f"/v1/my/projects/{project}/activity").json()["data"]
    assert next(row for row in history if row["action"] == "link_added")["detail"]["record_public_id"] == (
        delayed.public_id
    )


def test_history_names_no_id_of_someone_no_longer_a_member(browser: Browser, person: UserFactory) -> None:
    """A departed collaborator is "Former member" in the history, and their user id goes too
    (owner, 2026-10-22); a current member's id stays."""
    owner, editor = person(), person("editor@example.org")
    owner_tab, editor_tab = browser(owner), browser(editor)
    project = _create(owner_tab)["public_id"]
    _invite_and_join(owner_tab, project, editor_tab, "editor@example.org", "editor")
    assert (
        owner_tab.patch(
            f"/v1/my/projects/{project}/members/{editor.public_id}", json={"role": "viewer"}
        ).status_code
        == 200
    )
    before = owner_tab.get(f"/v1/my/projects/{project}/activity").json()["data"]
    changed = next(row for row in before if row["action"] == "role_changed")
    assert changed["detail"]["user_id"] == editor.public_id, "a current member's id is shown"
    assert owner_tab.delete(f"/v1/my/projects/{project}/members/{editor.public_id}").status_code == 204
    after = owner_tab.get(f"/v1/my/projects/{project}/activity")
    assert editor.public_id not in after.text
    by_action = {row["action"]: row for row in after.json()["data"]}
    for action in ("member_joined", "role_changed", "member_removed"):
        assert by_action[action]["detail"]["user_id"] is None, action
    assert by_action["member_removed"]["actor"]["user_public_id"] == owner.public_id


# ===================================================================================== contract
def test_responses_match_the_committed_contract(
    spec: dict[str, Any], browser: Browser, person: UserFactory, world: dict[str, Any]
) -> None:
    owner, guest = person(), person("guest@example.org")
    tab, guest_tab = browser(owner), browser(guest)
    created = tab.post(
        "/v1/my/projects",
        json={"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}},
    )
    assert_valid(spec, "MyProjectResponse", created.json())
    project = created.json()["data"]["public_id"]
    tab.post(
        f"/v1/my/projects/{project}/links",
        json={"record_type": "asset", "public_id": world["plant"].public_id, "relation": "nearby"},
    )
    invitation = tab.post(
        f"/v1/my/projects/{project}/invitations", json={"email": "guest@example.org", "role": "viewer"}
    )
    assert_valid(spec, "MyInvitationCreatedResponse", invitation.json())
    token = invitation.json()["data"]["link"].split("t=", 1)[1]
    note = tab.post(f"/v1/my/projects/{project}/notes", json={"body": "hi"})
    assert_valid(spec, "MyNoteResponse", note.json())
    checklist = tab.get(f"/v1/my/projects/{project}/checklist")
    assert_valid(spec, "MyChecklistResponse", checklist.json())
    item = _interconnection_item(checklist.json()["data"])
    decision = tab.post(
        f"/v1/my/projects/{project}/checklist/{item['item_id']}/evidence/{item['suggestions'][0]['evidence_id']}/accept"
    )
    assert_valid(spec, "MyEvidenceDecisionResponse", decision.json())
    status = tab.patch(
        f"/v1/my/projects/{project}/checklist/{item['item_id']}", json={"status": "in_progress"}
    )
    assert_valid(spec, "MyChecklistItemResponse", status.json())
    assert_valid(
        spec,
        "MyInvitationPreviewResponse",
        guest_tab.post("/v1/my/invitations/preview", json={"token": token}).json(),
    )
    assert_valid(
        spec,
        "MyInvitationAcceptedResponse",
        guest_tab.post("/v1/my/invitations/accept", json={"token": token}).json(),
    )
    tab.patch(f"/v1/my/projects/{project}", json={"lat": 30.0, "lon": -97.0})
    for path, schema in (
        ("/v1/my/projects", "MyProjectListResponse"),
        (f"/v1/my/projects/{project}", "MyProjectResponse"),
        (f"/v1/my/projects/{project}/links", "MyLinkListResponse"),
        (f"/v1/my/projects/{project}/notes", "MyNoteListResponse"),
        (f"/v1/my/projects/{project}/members", "MyMemberListResponse"),
        (f"/v1/my/projects/{project}/invitations", "MyInvitationListResponse"),
        (f"/v1/my/projects/{project}/activity", "MyActivityListResponse"),
        ("/v1/my/pins", "MyPinsResponse"),
    ):
        resp = tab.get(path)
        assert resp.status_code == 200, path
        assert_valid(spec, schema, resp.json())
    prefill = tab.post(
        "/v1/my/prefill", json={"record_type": "proposal", "public_id": world["proposal"].public_id}
    )
    assert_valid(spec, "MyPrefillResponse", prefill.json())
    forbidden = guest_tab.get(f"/v1/my/projects/{project}/invitations")
    assert_valid(spec, "Problem", forbidden.json())


def test_every_route_is_documented_session_only_under_private_projects(spec: dict[str, Any]) -> None:
    methods = {"get", "post", "patch", "put", "delete"}
    documented = {
        (method.upper(), path): op
        for path, item in spec["paths"].items()
        if path.startswith("/v1/my")
        for method, op in item.items()
        if method in methods
    }
    served = {
        (method, route.path)
        for route in my_projects.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert served == set(documented)
    for key, op in documented.items():
        assert op["tags"] == ["Private projects"], key
        assert op["security"] == [{"Session": []}], key
        statuses = set(op["responses"])
        assert "404" in statuses or "{project_id}" not in key[1], key


def test_error_codes_match_the_contract_and_forbidden_role_is_one(spec: dict[str, Any]) -> None:
    assert set(spec["components"]["schemas"]["ErrorCode"]["enum"]) == set(ERROR_CODES)
    assert ERROR_CODES["forbidden_role"] == 403
    pattern = spec["components"]["schemas"]["Problem"]["properties"]["type"]["pattern"]
    assert re.fullmatch(pattern, "https://api.inframation.io/errors/forbidden_role")


def test_the_analytics_refusal_carries_the_private_id_pattern() -> None:
    """`services/api/ui_events.py` may not import the private package, so it keeps a copy."""
    assert ui_events.PRIVATE_ID_PATTERN.pattern == pm.PRIVATE_ID_PATTERN.pattern


def test_evidence_rule_sources_are_registered_sources() -> None:
    registry = yaml.safe_load((REPO_ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    rows = registry["sources"] if isinstance(registry, dict) else registry
    ids = {row["id"] for row in rows}
    named = {
        my_projects.TEC_REGISTER_SOURCE,
        *my_projects.AIR_PERMIT_SOURCES,
        *my_projects.FEDERAL_RECORD_SOURCES,
    }
    assert named <= ids, sorted(named - ids)


# =========================================================================== the tracker (kinds)
def _notice(db: Session, source: Source, suffix: str, *, due_in_days: float | None, kind: str = "rfp") -> Any:
    """An open notice due `due_in_days` from now (`None`: no due date)."""
    opp = make_visible_opportunity(db, source, public_id_suffix=suffix)
    opp.kind = kind
    opp.due_at = dt.datetime.now(UTC) + dt.timedelta(days=due_in_days) if due_in_days is not None else None
    db.commit()
    return opp


def _change_event(
    db: Session,
    subject: Any,
    source: Source,
    *,
    subject_type: str,
    changed_keys: list[str],
    shown_at: dt.datetime,
    event_type: str = "status_change",
) -> Event:
    event = Event(
        subject_type=subject_type,
        subject_id=subject.id,
        event_type=event_type,
        observed_at=shown_at,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=shown_at,
        licence_id=source.licence_id,
        before=None,
        after=None,
        changed_keys=changed_keys,
        public_at=shown_at,
        published_at=shown_at,
        idempotency_key=f"tracker-test:{uuid.uuid4()}",
    )
    db.add(event)
    db.flush()
    db.commit()
    return event


def _membership(db: Session, project_public_id: str, user: User) -> pm.PrivateProjectMember:
    member = db.scalar(
        select(pm.PrivateProjectMember)
        .join(pm.PrivateProject, pm.PrivateProject.id == pm.PrivateProjectMember.project_id)
        .where(pm.PrivateProject.public_id == project_public_id, pm.PrivateProjectMember.user_id == user.id)
        .execution_options(populate_existing=True)
    )
    assert member is not None
    return member


def _rows(tab: TestClient, query: str = "") -> dict[str, dict[str, Any]]:
    resp = tab.get(f"/v1/my/projects{query}")
    assert resp.status_code == 200, resp.text
    return {row["title"]: row for row in resp.json()["data"]}


def test_track_makes_the_kind_each_record_offers(
    spec: dict[str, Any], browser: Browser, person: UserFactory, world: dict[str, Any]
) -> None:
    """A notice makes a bid; a project page a project or an investment; a plant a project. The
    kind is checked against the record before anything is read or stored."""
    tab = browser(person())
    opportunity = {"record_type": "opportunity", "public_id": world["opportunity"].public_id}
    proposal = {"record_type": "proposal", "public_id": world["proposal"].public_id}
    bid = _create(tab, title=None, **{"from": opportunity})
    assert (bid["kind"], bid["stage"], bid["title"]) == ("bid", "considering", "Bid: Test RFP 1")
    assert (bid["prefill"]["kind"], bid["prefill"]["kinds"]) == ("bid", ["bid"])
    assert bid["links"][0]["relation"] == "offtake_or_rfp"
    refused = tab.post("/v1/my/projects", json={"title": "x", "kind": "project", "from": opportunity})
    assert refused.status_code == 400 and refused.json()["errors"][0]["field"] == "kind"
    investment = _create(tab, title=None, kind="investment", **{"from": proposal})
    assert (investment["kind"], investment["stage"]) == ("investment", "screening")
    assert investment["links"][0]["relation"] == "this_project"
    assert "stage" not in investment["prefill"]["fields"]
    project = _create(tab, title=None, **{"from": proposal})
    assert (project["kind"], project["stage"]) == ("project", "development")
    plant = _create(tab, **{"from": {"record_type": "asset", "public_id": world["plant"].public_id}})
    assert plant["kind"] == "project"
    wrong = tab.post(
        "/v1/my/projects",
        json={
            "title": "x",
            "kind": "bid",
            "from": {"record_type": "asset", "public_id": world["plant"].public_id},
        },
    )
    assert wrong.status_code == 400
    preview = tab.post("/v1/my/prefill", json={**proposal, "kind": "investment"})
    assert_valid(spec, "MyPrefillResponse", preview.json())
    data = preview.json()["data"]
    assert (data["kind"], data["kinds"], data["relations"]) == (
        "investment",
        ["project", "investment"],
        ["this_project"],
    )
    assert "stage" not in data["values"]
    assert tab.post("/v1/my/prefill", json={**proposal, "kind": "bid"}).status_code == 400
    assert my_projects._track_kind("site", None) == "project"
    assert my_projects.START_RELATIONS["site"] == ("candidate_site", "nearby")


def test_the_tracker_sorts_by_next_deadline_then_by_latest_change(
    spec: dict[str, Any], browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    """A deadline is the soonest upcoming `due_at` of a linked notice; a project also takes its
    open bids'. A closed item, a deadline in the past and an undated notice give none. Items with a
    deadline come first, soonest first; the rest follow, most recently changed first."""
    source = world["source"]
    soon = _notice(db, source, "11", due_in_days=3)
    later = _notice(db, source, "12", due_in_days=10, kind="foa")
    far = _notice(db, source, "13", due_in_days=40)
    past = _notice(db, source, "14", due_in_days=-2)
    undated = _notice(db, source, "15", due_in_days=None)
    tab = browser(person())

    def track(notice: Any, **body: Any) -> dict[str, Any]:
        return _create(tab, **{"from": {"record_type": "opportunity", "public_id": notice.public_id}}, **body)

    soon_project = _create(tab, title="Soon project")
    linked = tab.post(
        f"/v1/my/projects/{soon_project['public_id']}/links",
        json={"record_type": "opportunity", "public_id": soon.public_id, "relation": "offtake_or_rfp"},
    )
    assert linked.status_code == 201, linked.text
    later_bid = track(later, title="Later bid")
    assert later_bid["links"][0]["relation"] == "funding"
    parent = _create(tab, title="Parent project")
    far_bid = track(far, title="Far bid", parent_project_id=parent["public_id"])
    track(soon, title="Won bid", stage="won")
    track(past, title="Past bid")
    track(undated, title="Undated bid")
    _create(tab, title="Fund", kind="investment")

    resp = tab.get("/v1/my/projects")
    assert_valid(spec, "MyProjectListResponse", resp.json())
    rows = resp.json()["data"]
    titles = [row["title"] for row in rows]
    assert titles[:2] == ["Soon project", "Later bid"]
    assert set(titles[2:4]) == {"Parent project", "Far bid"}
    tail = rows[4:]
    assert {row["title"] for row in tail} == {"Won bid", "Past bid", "Undated bid", "Fund"}
    assert all(row["next_deadline"] is None for row in tail)
    stamps = [row["updated_at"] for row in tail]
    assert stamps == sorted(stamps, reverse=True)
    by_title = {row["title"]: row for row in rows}
    soon_deadline = by_title["Soon project"]["next_deadline"]
    assert soon_deadline["record"]["public_id"] == soon.public_id and soon_deadline["days_left"] == 3
    assert soon_deadline["via_bid"] is None
    via = by_title["Parent project"]["next_deadline"]
    assert via["record"]["public_id"] == far.public_id
    assert via["via_bid"]["public_id"] == far_bid["public_id"]
    assert by_title["Won bid"]["closed"] is True and by_title["Won bid"]["stage_label"] == "Won"

    # The filter narrows the rows, never the deadlines: a project still takes its bid's.
    projects = _rows(tab, "?kind=project")
    assert set(projects) == {"Soon project", "Parent project"}
    assert projects["Parent project"]["next_deadline"]["via_bid"]["public_id"] == far_bid["public_id"]
    assert set(_rows(tab, "?kind=bid,investment")) == {
        "Later bid",
        "Far bid",
        "Won bid",
        "Past bid",
        "Undated bid",
        "Fund",
    }
    assert tab.get("/v1/my/projects?kind=portfolio").status_code == 400

    # The project's own page names its bids, each with its deadline, and the bid names its project.
    detail = tab.get(f"/v1/my/projects/{parent['public_id']}")
    assert_valid(spec, "MyProjectResponse", detail.json())
    (named,) = detail.json()["data"]["bids"]
    assert named["public_id"] == far_bid["public_id"]
    assert named["next_deadline"]["record"]["public_id"] == far.public_id
    bid_detail = tab.get(f"/v1/my/projects/{far_bid['public_id']}").json()["data"]
    assert bid_detail["parent"]["public_id"] == parent["public_id"]
    assert [stage["stage"] for stage in bid_detail["stages"]][:2] == ["considering", "preparing"]

    # A notice the viewer can no longer see gives no deadline.
    soon.public_at = dt.datetime.now(UTC) + dt.timedelta(days=5)
    db.commit()
    assert _rows(tab)["Soon project"]["next_deadline"] is None


def test_changes_since_your_last_visit_follow_each_member_and_the_public_feed(
    spec: dict[str, Any], browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    """A change counts when its public event became visible to the viewer's tier after they last
    opened the item (or joined it). Opening the item is the visit, for that member only."""
    owner, partner = person(), person("partner@example.org")
    tab, partner_tab = browser(owner), browser(partner)
    project = _create(
        tab,
        title="Watched",
        **{"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}},
    )
    bid = _create(
        tab,
        title="Bid",
        **{"from": {"record_type": "opportunity", "public_id": world["opportunity"].public_id}},
    )
    _invite_and_join(tab, project["public_id"], partner_tab, "partner@example.org", "viewer")
    now = dt.datetime.now(UTC)
    for public, user in (
        (project["public_id"], owner),
        (project["public_id"], partner),
        (bid["public_id"], owner),
    ):
        member = _membership(db, public, user)
        member.added_at = now - dt.timedelta(days=3)
    db.commit()
    source = world["source"]
    other = make_visible_proposal(db, source, public_id_suffix="7")
    db.commit()
    _change_event(
        db,
        world["proposal"],
        source,
        subject_type="proposal",
        changed_keys=["lifecycle_state"],
        shown_at=now - dt.timedelta(days=2),
    )
    _change_event(
        db,
        world["proposal"],
        source,
        subject_type="proposal",
        changed_keys=["capacity_mw"],
        shown_at=now - dt.timedelta(days=4),
    )
    _change_event(
        db,
        world["proposal"],
        source,
        subject_type="proposal",
        changed_keys=["lifecycle_state"],
        shown_at=now + dt.timedelta(days=1),
    )
    _change_event(
        db,
        other,
        source,
        subject_type="proposal",
        changed_keys=["lifecycle_state"],
        shown_at=now - dt.timedelta(days=1),
    )
    _change_event(
        db,
        world["opportunity"],
        source,
        subject_type="opportunity",
        changed_keys=["due_at"],
        event_type="field_change",
        shown_at=now - dt.timedelta(days=1),
    )

    rows = _rows(tab)
    assert rows["Watched"]["changed_since_last_visit"]["changed"] is True
    assert (
        rows["Watched"]["changed_since_last_visit"]["reason"],
        rows["Watched"]["changed_since_last_visit"]["count"],
    ) == ("status", 1)
    assert rows["Watched"]["changed_since_last_visit"]["text"] == "Status changed"
    assert rows["Bid"]["changed_since_last_visit"]["reason"] == "deadline"
    assert rows["Bid"]["changed_since_last_visit"]["text"] == "Deadline moved"

    opened = tab.get(f"/v1/my/projects/{project['public_id']}")
    assert_valid(spec, "MyProjectResponse", opened.json())
    data = opened.json()["data"]
    (change,) = data["changes"]
    assert change["record"]["public_id"] == world["proposal"].public_id and change["reason"] == "status"
    assert change["source"]["source_id"] == source.id
    assert data["last_viewed_at"] is None
    assert _membership(db, project["public_id"], owner).last_viewed_at is not None
    # The visit is the owner's own: the partner still sees the change; the bid was not opened.
    assert _rows(tab)["Watched"]["changed_since_last_visit"]["changed"] is False
    assert _rows(tab)["Bid"]["changed_since_last_visit"]["changed"] is True
    assert _rows(partner_tab)["Watched"]["changed_since_last_visit"]["changed"] is True
    again = tab.get(f"/v1/my/projects/{project['public_id']}").json()["data"]
    assert again["changes"] == [] and again["last_viewed_at"] is not None

    # A change after the visit shows again.
    member = _membership(db, project["public_id"], owner)
    member.last_viewed_at = now - dt.timedelta(hours=12)
    db.commit()
    _change_event(
        db,
        world["proposal"],
        source,
        subject_type="proposal",
        changed_keys=["iso"],
        shown_at=now - dt.timedelta(hours=6),
        event_type="field_change",
    )
    badge = _rows(tab)["Watched"]["changed_since_last_visit"]
    assert (badge["changed"], badge["reason"], badge["text"]) == (True, "updated", "Record updated")


def test_email_changes_is_each_members_own_opt_in(browser: Browser, person: UserFactory) -> None:
    owner, viewer = person(), person("viewer@example.org")
    tab, viewer_tab = browser(owner), browser(viewer)
    project = _create(tab)["public_id"]
    _invite_and_join(tab, project, viewer_tab, "viewer@example.org", "viewer")
    assert tab.get(f"/v1/my/projects/{project}").json()["data"]["email_changes"] is False
    opted = viewer_tab.patch(f"/v1/my/projects/{project}", json={"email_changes": True})
    assert opted.status_code == 200 and opted.json()["data"]["email_changes"] is True
    assert tab.get(f"/v1/my/projects/{project}").json()["data"]["email_changes"] is False
    assert viewer_tab.patch(f"/v1/my/projects/{project}", json={"email_changes": "yes"}).status_code == 400
    both = viewer_tab.patch(f"/v1/my/projects/{project}", json={"email_changes": False, "title": "Mine now"})
    assert both.status_code == 403
    # The refused request wrote nothing: the opt-in it carried was rolled back with it.
    assert viewer_tab.get(f"/v1/my/projects/{project}").json()["data"]["email_changes"] is True
    invalid = tab.patch(f"/v1/my/projects/{project}", json={"email_changes": True, "stage": "won"})
    assert invalid.status_code == 400
    assert tab.get(f"/v1/my/projects/{project}").json()["data"]["email_changes"] is False
    history = tab.get(f"/v1/my/projects/{project}/activity").json()["data"]
    assert {row["action"] for row in history} <= {"created", "member_invited", "member_joined"}


def test_kinds_are_served_and_an_edit_keeps_its_kind(
    spec: dict[str, Any], browser: Browser, person: UserFactory
) -> None:
    owner, outsider = person(), person()
    tab, outsider_tab = browser(owner), browser(outsider)
    vocabulary = tab.get("/v1/my/kinds")
    assert vocabulary.status_code == 200
    assert_valid(spec, "MyKindsResponse", vocabulary.json())
    served = vocabulary.json()["data"]
    assert [kind["kind"] for kind in served["kinds"]] == ["project", "bid", "investment"]
    assert served["track"]["opportunity"] == ["bid"]
    assert served["track"]["proposal"] == ["project", "investment"]
    assert [stage["stage"] for stage in served["kinds"][2]["stages"]] == [
        "screening",
        "diligence",
        "term_sheet",
        "committed",
        "closed",
        "passed",
    ]
    project = _create(tab, title="Project")["public_id"]
    bid = _create(tab, title="Bid", kind="bid")
    assert (bid["stage"], bid["submitted_on"], bid["parent"]) == ("considering", None, None)
    resp = tab.patch(f"/v1/my/projects/{project}", json={"kind": "bid"})
    assert resp.status_code == 400 and resp.json()["errors"][0]["field"] == "kind"
    assert tab.patch(f"/v1/my/projects/{bid['public_id']}", json={"stage": "development"}).status_code == 400
    assert tab.patch(f"/v1/my/projects/{project}", json={"submitted_on": "2026-10-20"}).status_code == 400
    edited = tab.patch(
        f"/v1/my/projects/{bid['public_id']}",
        json={"stage": "submitted", "submitted_on": "2026-10-20", "parent_project_id": project},
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["data"]["submitted_on"], edited.json()["data"]["parent"]["public_id"]) == (
        "2026-10-20",
        project,
    )
    theirs = _create(outsider_tab, title="Theirs")["public_id"]
    refusals = {
        tab.patch(f"/v1/my/projects/{bid['public_id']}", json={"parent_project_id": other}).json()["detail"]
        for other in (theirs, MISSING_PROJECT)
    }
    assert refusals == {authz.PARENT_REFUSED}


def test_a_bid_an_editor_attaches_to_the_owners_project_stays_the_editors_own(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    """A bid is private to its own members even when it names a project someone else owns: that
    project's owner never sees it, in the project's bids, its deadline, its history, its people or
    the map pins, unless the bid's members invite them to the bid."""
    owner, editor = person(), person("editor@example.org")
    owner_tab, editor_tab = browser(owner), browser(editor)
    project = _create(owner_tab, title="Owner project")["public_id"]
    _invite_and_join(owner_tab, project, editor_tab, "editor@example.org", "editor")
    notice = _notice(db, world["source"], "21", due_in_days=4)
    bid = _create(
        editor_tab,
        title="QZV unshared bid",
        parent_project_id=project,
        **{"from": {"record_type": "opportunity", "public_id": notice.public_id}},
    )
    assert bid["kind"] == "bid" and bid["parent"]["public_id"] == project
    theirs = editor_tab.get(f"/v1/my/projects/{project}").json()["data"]
    assert [row["public_id"] for row in theirs["bids"]] == [bid["public_id"]]
    assert theirs["next_deadline"]["via_bid"]["public_id"] == bid["public_id"]
    for path in (
        f"/v1/my/projects/{project}",
        "/v1/my/projects",
        "/v1/my/projects?kind=project",
        f"/v1/my/projects/{project}/activity",
        f"/v1/my/projects/{project}/members",
        "/v1/my/pins",
    ):
        resp = owner_tab.get(path)
        assert resp.status_code == 200, (path, resp.text)
        assert "QZV" not in resp.text and bid["public_id"] not in resp.text, path
    seen = owner_tab.get(f"/v1/my/projects/{project}").json()["data"]
    assert seen["bids"] == [] and seen["next_deadline"] is None
    assert _rows(owner_tab)["Owner project"]["next_deadline"] is None
    assert owner_tab.get(f"/v1/my/projects/{bid['public_id']}").status_code == 404


def test_an_archived_bid_is_marked_on_its_project_and_gives_it_no_deadline(
    spec: dict[str, Any], browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    """An archived bid is off the tracker, so its deadline is not its project's, on the list (with
    or without archived items) and on the project's own page alike; the page still names the bid,
    marked archived."""
    tab = browser(person())
    notice = _notice(db, world["source"], "22", due_in_days=6)
    project = _create(tab, title="Parent")["public_id"]
    bid = _create(
        tab,
        title="Archived bid",
        parent_project_id=project,
        **{"from": {"record_type": "opportunity", "public_id": notice.public_id}},
    )
    via = tab.get(f"/v1/my/projects/{project}").json()["data"]["next_deadline"]["via_bid"]
    assert via["public_id"] == bid["public_id"]
    assert tab.patch(f"/v1/my/projects/{bid['public_id']}", json={"archived": True}).status_code == 200
    detail = tab.get(f"/v1/my/projects/{project}")
    assert_valid(spec, "MyProjectResponse", detail.json())
    data = detail.json()["data"]
    (listed,) = data["bids"]
    assert (listed["public_id"], listed["archived"]) == (bid["public_id"], True)
    assert data["next_deadline"] is None
    assert _rows(tab)["Parent"]["next_deadline"] is None
    assert _rows(tab, "?include_archived=true")["Parent"]["next_deadline"] is None
    assert tab.get(f"/v1/my/projects/{bid['public_id']}").json()["data"]["parent"]["archived"] is False
    assert tab.patch(f"/v1/my/projects/{bid['public_id']}", json={"archived": False}).status_code == 200
    assert _rows(tab)["Parent"]["next_deadline"]["via_bid"]["public_id"] == bid["public_id"]


def test_a_public_record_reads_the_same_whether_or_not_anyone_tracks_it(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    browser: Browser,
    person: UserFactory,
    world: dict[str, Any],
) -> None:
    """No public answer varies with tracking (decision row 2026-10-23: no page ever shows how many
    users track a record). The record's API responses are byte-identical before and after two users
    track it, anonymously and for a third signed-in user."""
    monkeypatch.setattr("services.api.serialize.utcnow", lambda: dt.datetime(2026, 10, 23, 12, 0, tzinfo=UTC))
    monkeypatch.setattr("services.api.request_context.new_request_id", lambda: "req_fixed0000")
    reader = browser(person())
    paths = (
        f"/v1/proposals/{world['proposal'].public_id}",
        f"/v1/proposals/{world['proposal'].public_id}/events",
        f"/v1/opportunities/{world['opportunity'].public_id}",
        f"/v1/assets/{world['plant'].public_id}",
        "/v1/proposals",
        "/v1/opportunities",
    )

    def snapshot() -> list[bytes]:
        out = []
        for path in paths:
            for caller in (client, reader):
                resp = caller.get(path)
                assert resp.status_code == 200, (path, resp.text)
                out.append(resp.content)
        return out

    before = snapshot()
    for user in (person(), person()):
        tab = browser(user)
        _create(tab, **{"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}})
        _create(
            tab,
            kind="investment",
            **{"from": {"record_type": "proposal", "public_id": world["proposal"].public_id}},
        )
        _create(tab, **{"from": {"record_type": "opportunity", "public_id": world["opportunity"].public_id}})
        _create(tab, **{"from": {"record_type": "asset", "public_id": world["plant"].public_id}})
    assert snapshot() == before


def test_track_from_a_site_makes_a_project_at_the_site(
    browser: Browser, person: UserFactory, db: Session, world: dict[str, Any]
) -> None:
    """A site (one developer's records at one grid point) makes a project: its title names the
    site's lead record and its map point is the lead's, both under the licence rule."""
    from services.api.conftest import make_org
    from services.db.models import Site
    from services.sites import build

    source = world["source"]
    point = InterconnectionPoint(
        public_id="",
        operator="ERCOT",
        name_display="Bearkat 345kV",
        name_key="sub:bearkat|345",
        key_rule="test",
        kind="substation",
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 10, 1, tzinfo=UTC),
        licence_id=world["licence"].id,
    )
    db.add(point)
    db.flush()
    point.public_id = public_id("poi", point.id)
    developer = make_org(db, "Kestrel Renewables LLC")
    location = make_location(db, source, world["licence"], geom=(-101.0, 32.5), precision="exact")
    for suffix, name, mw in (("21", "Kestrel Solar 2", 400), ("22", "Kestrel Solar 1", 200)):
        member = make_visible_proposal(
            db, source, public_id_suffix=suffix, sponsor=developer, location=location
        )
        member.name_canonical = name
        member.capacity_mw = mw
        member.interconnection_point_id = point.id
    db.flush()
    build.rebuild_sites(db)
    db.commit()
    site = db.scalars(select(Site)).first()
    assert site is not None
    tab = browser(person())
    item = _create(tab, title=None, **{"from": {"record_type": "site", "public_id": site.public_id}})
    assert item["kind"] == "project" and item["title"].startswith("Project at Kestrel Solar")
    assert (item["lat"], item["lon"], item["point_precision"]) == (32.5, -101.0, "exact")
    assert item["links"][0]["relation"] == "candidate_site" and item["links"][0]["state"] == "available"
    refused = tab.post(
        "/v1/my/projects",
        json={"title": "x", "kind": "bid", "from": {"record_type": "site", "public_id": site.public_id}},
    )
    assert refused.status_code == 400
