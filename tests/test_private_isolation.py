"""Nothing private reaches a public surface (docs/19 §5.2, §5.3, §5.6; ADR 0009 decision 2).

The canary: private projects whose every field holds a sentinel string (title, note, custom
checklist item, invitation address) and a sentinel map point, linked to public records, shared with
a collaborator and with an invitation pending. Then:

- every `GET` operation in `api/openapi.yaml` outside `/v1/my/` is fetched anonymously, with the
  owner's session and with the owner's API key, and every `/admin/v1/` one with an operator's
  session; every feed, sitemap, `robots.txt`, the search and list pages and the record pages (with
  their JSON-LD and Open Graph tags) of the site are fetched signed out and signed in. The sentinel,
  the point, the private ids and the invitation token appear in none of them;
- the public lists, maps, counts and coverage answer byte for byte as before the projects existed;
- a full private session writes no `event` row (the public feed, alerts, webhooks, social drafts
  and the admin audit log all read that table);
- the process's own logs, through the production formatter and filter, and its error reports,
  through the real pinned sentry-sdk and `infra.observability.before_send`, carry none of it, a
  forced exception inside a private route included.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from infra.logging_config import configure_logging
from services.api import assets as asset_routes
from services.api import geo_cache
from services.api.app import app as api_app
from services.api.auth import create_session
from services.api.conftest import (
    make_asset,
    make_event,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.ratelimit import default_limiter
from services.db.models import Event, InterconnectionPoint, User
from services.ids import public_id
from services.projects import checklist as private_checklist
from services.projects import models as pm
from tests.conftest import make_account, make_api_key, make_user

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SENTINEL = "QZV-private-canary-4417"
SENTINEL_EMAIL = "qzv-canary-4417@example.org"
SENTINEL_LAT, SENTINEL_LON = 12.345678, -98.765432
ORIGIN = "http://testserver"


@pytest.fixture(autouse=True)
def _pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-private-isolation")


def _user(db: Session, email: str, *, entitlement: str = "public", role: str = "member") -> User:
    account = make_account(db, entitlement=entitlement, name=f"Account {email}")
    user = make_user(db, account, email=email, role=role)
    user.email_verified_at = dt.datetime(2026, 10, 1, tzinfo=UTC)
    db.commit()
    return user


def _tab(db: Session, user: User) -> TestClient:
    _row, cookie = create_session(db, user)
    db.commit()
    tab = TestClient(api_app)
    tab.headers["Origin"] = ORIGIN
    tab.cookies.set("session", cookie)
    return tab


@pytest.fixture()
def public_world(client: TestClient, db: Session) -> dict[str, Any]:
    """The public records; requesting `client` installs the app's database override for every
    `_tab` the tests open."""
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    sponsor = make_org(db, "Acme Power LLC")
    location = make_location(db, source, licence, geom=(-97.7, 30.3), precision="exact")
    proposal = make_visible_proposal(db, source, sponsor=sponsor, location=location)
    second = make_visible_proposal(db, source, public_id_suffix="2")
    make_event(db, proposal, source)
    opportunity = make_visible_opportunity(db, source)
    plant = make_asset(db, source, licence, status="retiring", name="Old Plant")
    point = InterconnectionPoint(
        public_id="",
        operator="ERCOT",
        name_display="Bearkat 345kV",
        name_key="sub:bearkat|345",
        key_rule="test",
        kind="substation",
        voltage_kv=345,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 10, 1, tzinfo=UTC),
        licence_id=licence.id,
    )
    db.add(point)
    db.flush()
    point.public_id = public_id("poi", point.id)
    proposal.interconnection_point_id = point.id
    db.commit()
    return {
        "licence": licence,
        "source": source,
        "sponsor": sponsor,
        "proposal": proposal,
        "second": second,
        "opportunity": opportunity,
        "plant": plant,
        "point": point,
    }


@pytest.fixture()
def canary(db: Session, public_world: dict[str, Any]) -> dict[str, Any]:
    """Two private projects full of sentinels, made through the API as a user would."""
    owner = _user(db, "owner@example.org", entitlement="api")
    collaborator = _user(db, "partner@example.org")
    owner_tab, partner_tab = _tab(db, owner), _tab(db, collaborator)
    created = owner_tab.post(
        "/v1/my/projects",
        json={
            "title": f"Deal {SENTINEL}",
            "lat": SENTINEL_LAT,
            "lon": SENTINEL_LON,
            "capacity_mw": 4417.0,
            "from": {"record_type": "proposal", "public_id": public_world["proposal"].public_id},
        },
    )
    assert created.status_code == 201, created.text
    project = created.json()["data"]["public_id"]
    second = owner_tab.post("/v1/my/projects", json={"title": f"Side {SENTINEL}"}).json()["data"]["public_id"]
    for record_type, record_id, relation in (
        ("opportunity", public_world["opportunity"].public_id, "offtake_or_rfp"),
        ("asset", public_world["plant"].public_id, "grid_connection"),
        ("organization", public_world["sponsor"].public_id, "counterparty"),
        ("interconnection_point", public_world["point"].public_id, "grid_connection"),
        ("proposal", public_world["second"].public_id, "comparable"),
    ):
        resp = owner_tab.post(
            f"/v1/my/projects/{project}/links",
            json={"record_type": record_type, "public_id": record_id, "relation": relation},
        )
        assert resp.status_code == 201, resp.text
    note = owner_tab.post(f"/v1/my/projects/{project}/notes", json={"body": f"Note {SENTINEL}\nline two"})
    item = owner_tab.post(
        f"/v1/my/projects/{project}/checklist", json={"name": f"Item {SENTINEL}", "folder": "permits"}
    )
    owner_tab.patch(
        f"/v1/my/projects/{project}/checklist/{item.json()['data']['item_id']}", json={"status": "done"}
    )
    owner_tab.get(f"/v1/my/projects/{project}/checklist")  # records suggestion references
    pending = owner_tab.post(
        f"/v1/my/projects/{project}/invitations", json={"email": SENTINEL_EMAIL, "role": "viewer"}
    )
    joined = owner_tab.post(
        f"/v1/my/projects/{project}/invitations", json={"email": "partner@example.org", "role": "editor"}
    )
    token = joined.json()["data"]["link"].split("t=", 1)[1]
    assert partner_tab.post("/v1/my/invitations/accept", json={"token": token}).status_code == 200
    partner_tab.post(f"/v1/my/projects/{project}/notes", json={"body": f"Partner {SENTINEL}"})
    pending_token = pending.json()["data"]["link"].split("t=", 1)[1]
    secrets_ = {
        SENTINEL,
        SENTINEL_EMAIL,
        f"{SENTINEL_LAT}",
        f"{SENTINEL_LON}"[1:],
        "4417.0",
        project,
        second,
        note.json()["data"]["note_id"],
        pending.json()["data"]["invitation_id"],
        pending_token,
        token,
    }
    owner_tab.close()
    partner_tab.close()
    return {"owner": owner, "collaborator": collaborator, "project": project, "secrets": secrets_}


def _leaks(text: str, secrets_: set[str]) -> list[str]:
    lowered = text.lower()
    return sorted(s for s in secrets_ if s.lower() in lowered)


# ======================================================================== the public surfaces
def _fill(path: str, ids: dict[str, str]) -> str:
    by_collection = {
        "proposals": ids["proposal"],
        "opportunities": ids["opportunity"],
        "organizations": ids["organization"],
        "assets": ids["asset"],
        "interconnection-points": ids["point"],
        "sites": "site_0000000000",
    }

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "public_id":
            collection = next(part for part in path.split("/") if part in by_collection)
            return by_collection[collection]
        return ids.get(name, f"{name}-0000000000")

    return re.sub(r"\{([a-z_]+)\}", one, path)


def _get_operations() -> list[str]:
    spec = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    return sorted(
        path for path, item in spec["paths"].items() if "get" in item and not path.startswith("/v1/my")
    )


@pytest.fixture()
def web(client: TestClient) -> Iterator[TestClient]:
    from web.api_client import ApiClient
    from web.app import app as web_app

    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as site:
        yield site
    del web_app.state.api_client


def test_sentinel_never_on_public_surfaces(
    client: TestClient, db: Session, web: TestClient, public_world: dict[str, Any], canary: dict[str, Any]
) -> None:
    secrets_ = canary["secrets"]
    owner: User = canary["owner"]
    operator = _user(db, "operator@example.org", role="operator")
    _key, api_key = make_api_key(
        db, owner.account, owner, scopes=["read:live", "read:bulk", "write:webhooks"]
    )
    db.commit()
    event_ids = [e["id"] for e in client.get("/v1/events").json()["data"]] or ["evt_0000000000"]
    ids = {
        "proposal": public_world["proposal"].public_id,
        "opportunity": public_world["opportunity"].public_id,
        "organization": public_world["sponsor"].public_id,
        "asset": public_world["plant"].public_id,
        "point": public_world["point"].public_id,
        "event_id": event_ids[0],
        "source_id": public_world["source"].id,
        "licence_id": public_world["licence"].id,
        "user_id": owner.public_id,
        "account_id": owner.account.public_id,
        "format": "json",
        "number": "1",
        "record_type": "proposal",
        "channel": "bluesky",
    }
    owner_session = _tab(db, owner).cookies.get("session")
    operator_session = _tab(db, operator).cookies.get("session")
    credentials: dict[str, dict[str, Any]] = {
        "anonymous": {},
        "owner session": {"cookies": {"session": owner_session}},
        "owner key": {"headers": {"Authorization": f"Bearer {api_key}"}},
    }
    fetched = answered = 0
    leaks: list[tuple[str, str, list[str]]] = []
    for template in _get_operations():
        paths = [_fill(template, ids)]
        if "{format}" in template:
            paths.append(_fill(template, {**ids, "format": "rss"}))
        for path in paths:
            target = web if path.startswith("/sitemap") else client
            who = (
                {"operator": {"cookies": {"session": operator_session}}}
                if path.startswith("/admin/")
                else credentials
            )
            for label, kwargs in who.items():
                default_limiter.reset()
                for name, value in (kwargs.get("cookies") or {}).items():
                    target.cookies.set(name, value)
                resp = target.get(path, headers=kwargs.get("headers"))
                target.cookies.clear()
                fetched += 1
                answered += resp.status_code == 200
                found = _leaks(resp.text + json.dumps(dict(resp.headers)), secrets_)
                if found:
                    leaks.append((label, path, found))
    # Not vacuous: most operations answered with data, on every credential.
    assert fetched > 200 and answered > 150, (fetched, answered)
    # Search: a private title is not a result anywhere.
    for path in ("/v1/proposals", "/v1/opportunities", "/v1/organizations", "/v1/assets"):
        default_limiter.reset()
        resp = client.get(path, params={"q": SENTINEL})
        assert resp.status_code == 200 and resp.json()["data"] == [], path
    # The site: lists, search, record pages (JSON-LD and Open Graph included), feeds, sitemaps, robots.
    pages = [
        "/",
        "/map",
        "/proposals",
        # A page echoes its query, so it searches the sentinel's first word: a private title that
        # matched would print the whole sentinel.
        "/proposals?q=QZV",
        "/search?q=QZV",
        # Search shows one kind at a time (lane stacked-lists): every kind's view, not only the first.
        "/search?q=QZV&type=companies",
        "/search?q=QZV&type=projects",
        "/search?q=QZV&type=infrastructure",
        "/search?q=QZV&type=rfps",
        "/search?q=QZV&type=funding",
        f"/proposals/{public_world['proposal'].slug}",
        "/opportunities",
        f"/opportunities/{public_world['opportunity'].slug}",
        "/organizations",
        f"/organizations/{public_world['sponsor'].slug}",
        "/assets",
        f"/assets/{public_world['plant'].slug}",
        "/interconnection-points",
        f"/interconnection-points/{public_world['point'].public_id}",
        "/sitemap.xml",
        "/sitemaps/1.xml",
        "/robots.txt",
        "/api/proposals/geo?zoom=5&bbox=-130,20,-60,55",
        "/api/assets/geo?zoom=5&bbox=-130,20,-60,55",
    ]
    for page in pages:
        for signed_in in (False, True):
            default_limiter.reset()
            if signed_in:
                web.cookies.set("session", owner_session)
            resp = web.get(page)
            web.cookies.clear()
            assert resp.status_code < 500, (page, resp.status_code)
            found = _leaks(resp.text, secrets_)
            if found:
                leaks.append(("site, signed in" if signed_in else "site", page, found))
    record_page = web.get(f"/proposals/{public_world['proposal'].slug}").text
    assert "application/ld+json" in record_page or 'property="og:' in record_page, "JSON-LD or OG was checked"
    assert leaks == [], leaks


# ========================================================================== counts unchanged
def _normalised(body: Any) -> Any:
    if isinstance(body, dict):
        return {
            key: _normalised(value)
            for key, value in body.items()
            if key not in ("request_id", "generated_at", "data_as_of")
        }
    if isinstance(body, list):
        return [_normalised(value) for value in body]
    return body


COUNTED = (
    "/v1/proposals?include=count",
    "/v1/opportunities?include=count",
    "/v1/organizations?include=count",
    "/v1/assets",
    "/v1/interconnection-points?include=count",
    "/v1/proposals/geo?zoom=3&bbox=-130,20,-60,55",
    "/v1/proposals/geo?zoom=9&bbox=-98.5,29.5,-97,31",
    "/v1/opportunities/geo?zoom=3&bbox=-130,20,-60,55",
    "/v1/assets/geo?zoom=5&bbox=-130,20,-60,55",
    "/v1/coverage",
    "/v1/events",
    "/v1/organizations/{organization}/pipeline",
    "/v1/interconnection-points/{point}",
)


def _snapshot(client: TestClient, ids: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for template in COUNTED:
        path = template.format(**ids)
        default_limiter.reset()
        # Force every whole-set answer to be recomputed rather than served from a process cache: a
        # cached answer would pass this test whatever the private tables held.
        geo_cache._bump()
        asset_routes._reset_asset_index_cache()
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        out[path] = _normalised(resp.json())
    return out


def test_public_counts_unchanged_by_private_projects(
    client: TestClient, db: Session, public_world: dict[str, Any]
) -> None:
    ids = {"organization": public_world["sponsor"].public_id, "point": public_world["point"].public_id}
    before = _snapshot(client, ids)
    owner = _user(db, "counter@example.org")
    tab = _tab(db, owner)
    for n in range(5):
        project = tab.post(
            "/v1/my/projects",
            json={
                "lat": 30.3 + n / 100,
                "lon": -97.7,
                "from": {"record_type": "proposal", "public_id": public_world["proposal"].public_id},
            },
        ).json()["data"]["public_id"]
        tab.post(
            f"/v1/my/projects/{project}/links",
            json={
                "record_type": "interconnection_point",
                "public_id": public_world["point"].public_id,
                "relation": "grid_connection",
            },
        )
        tab.get(f"/v1/my/projects/{project}/checklist")
    assert db.scalar(select(func.count()).select_from(pm.PrivateProject)) == 5
    after = _snapshot(client, ids)
    assert after == before


# ================================================================================ no events
def test_private_operations_write_no_event(db: Session, public_world: dict[str, Any]) -> None:
    before = db.scalar(select(func.count()).select_from(Event))
    owner, partner = _user(db, "o@example.org"), _user(db, "p@example.org")
    tab, partner_tab = _tab(db, owner), _tab(db, partner)
    project = tab.post(
        "/v1/my/projects",
        json={"from": {"record_type": "proposal", "public_id": public_world["proposal"].public_id}},
    ).json()["data"]["public_id"]
    tab.post(f"/v1/my/projects/{project}/notes", json={"body": "n"})
    checklist = tab.get(f"/v1/my/projects/{project}/checklist").json()["data"]
    item = next(i for f in checklist["folders"] for i in f["items"] if i["suggestions"])
    tab.post(
        f"/v1/my/projects/{project}/checklist/{item['item_id']}/evidence/{item['suggestions'][0]['evidence_id']}/accept"
    )
    tab.patch(f"/v1/my/projects/{project}/checklist/{item['item_id']}", json={"status": "in_progress"})
    link = tab.post(
        f"/v1/my/projects/{project}/invitations", json={"email": "p@example.org", "role": "editor"}
    ).json()
    partner_tab.post("/v1/my/invitations/accept", json={"token": link["data"]["link"].split("t=", 1)[1]})
    tab.patch(f"/v1/my/projects/{project}/members/{partner.public_id}", json={"role": "owner"})
    partner_tab.patch(f"/v1/my/projects/{project}", json={"archived": True})
    partner_tab.patch(f"/v1/my/projects/{project}", json={"archived": False})
    tab.delete(f"/v1/my/projects/{project}/members/{owner.public_id}")
    assert partner_tab.delete(f"/v1/my/projects/{project}").status_code == 204
    assert db.scalar(select(func.count()).select_from(Event)) == before


# ======================================================================== logs, error reports
@pytest.fixture()
def json_log() -> Iterator[io.StringIO]:
    """The production handler and filter (`infra.logging_config.configure_logging`) on a buffer,
    removed afterwards so the rest of the suite logs as before."""
    root = logging.getLogger()
    level = root.level
    existing = [h for h in root.handlers if getattr(h, "_infraque_json", False)]
    for handler in existing:
        root.removeHandler(handler)
    buffer = io.StringIO()
    handler = configure_logging("DEBUG", stream=buffer)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        for old in existing:
            root.addHandler(old)
        root.setLevel(level)


def test_logs_carry_no_private_content(
    db: Session, public_world: dict[str, Any], json_log: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _user(db, "logger@example.org")
    tab = _tab(db, owner)
    resp = tab.post(
        "/v1/my/projects",
        json={"title": f"Deal {SENTINEL}", "lat": SENTINEL_LAT, "lon": SENTINEL_LON},
    )
    project = resp.json()["data"]["public_id"]
    tab.post(f"/v1/my/projects/{project}/notes", json={"body": f"Note {SENTINEL}"})
    tab.post(f"/v1/my/projects/{project}/invitations", json={"email": SENTINEL_EMAIL, "role": "viewer"})
    tab.post(f"/v1/my/projects/{project}/notes", json={"body": f"bad \x00 {SENTINEL}"})
    tab.patch(f"/v1/my/projects/{project}", json={"title": "x" * 300})

    def broken(db: Session, keys: Any) -> dict[str, str]:
        raise RuntimeError(f"guidance failed for {SENTINEL} at {SENTINEL_LAT} for {SENTINEL_EMAIL}")

    monkeypatch.setattr(private_checklist, "guidance_for", broken)
    failing = TestClient(api_app, raise_server_exceptions=False)
    failing.headers["Origin"] = ORIGIN
    failing.cookies.set("session", tab.cookies.get("session"))
    assert failing.get(f"/v1/my/projects/{project}/checklist").status_code == 500
    logging.getLogger("services.api.my_projects").info(
        "project %s note %s", project, SENTINEL, extra={"title": SENTINEL}
    )
    output = json_log.getvalue()
    assert "unhandled_error" in output and "RuntimeError" in output, "the forced exception was logged"
    secrets_ = {SENTINEL, SENTINEL_EMAIL, str(SENTINEL_LAT)}
    assert _leaks(output, secrets_) == []


#: The real API in a fresh interpreter with the real pinned sentry-sdk, initialised as production
#: initialises it, and a transport that keeps the events. A private route fails with the project's
#: title in its exception message. The sentinel arrives through the environment, so no source line
#: (which an event's frames carry) holds it.
PROBE = r"""
import datetime as dt, json, os, sys
import sentry_sdk
from sentry_sdk.transport import Transport

events = []


class Keep(Transport):
    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") == "event":
                events.append(item.payload.json)


real_init = sentry_sdk.init


def init(**kwargs):
    kwargs["transport"] = Keep(kwargs)
    return real_init(**kwargs)


sentry_sdk.init = init
from infra.observability import init_error_tracking

assert init_error_tracking("api")
from fastapi.testclient import TestClient

import services.projects.models
from services.api.app import app
from services.api.auth import create_session
from services.api.deps import get_db
from services.db.session import get_sessionmaker
from services.projects import checklist
from tests.conftest import make_account, make_user
from tests.db_template import fresh_engine

SECRET = os.environ["PROBE_SENTINEL"]
maker = get_sessionmaker(fresh_engine())


def session():
    db = maker()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


app.dependency_overrides[get_db] = session
db = maker()
user = make_user(db, make_account(db, entitlement="public"), email="probe@example.org")
user.email_verified_at = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
db.commit()
_row, cookie = create_session(db, user)
db.commit()
client = TestClient(app, raise_server_exceptions=False)
client.headers["Origin"] = "http://testserver"
client.cookies.set("session", cookie)
point = {"lat": 12.345678, "lon": -98.765432}
project = client.post("/v1/my/projects", json={"title": SECRET, **point}).json()["data"]["public_id"]
client.post(f"/v1/my/projects/{project}/notes", json={"body": SECRET})


def broken(db, keys):
    title = db.execute(__import__("sqlalchemy").text("select title from private_project")).scalar()
    raise RuntimeError(title)


checklist.guidance_for = broken
status = client.get(f"/v1/my/projects/{project}/checklist?q=1").status_code
status2 = client.get(f"/v1/my/projects/{project}/checklist").status_code
sentry_sdk.flush()
print(json.dumps({"events": events, "project": project, "status": [status, status2]}))
"""


def test_error_reports_carry_no_private_content(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("sentry_sdk")
    script = tmp_path / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("SENTRY_")},
        "PYTHONPATH": str(REPO_ROOT),
        "SENTRY_DSN": "https://publickey@o0.ingest.example.invalid/0",
        "PROBE_SENTINEL": SENTINEL,
        "AUDIT_HASH_PEPPER": "test-pepper-private-isolation",
    }
    result = subprocess.run(  # noqa: S603 -- this interpreter, a script this test wrote
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    report: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["status"] == [400, 500]
    events = report["events"]
    assert events, "the failure was reported"
    assert any((e.get("exception") or {}).get("values") for e in events), "with its exception"
    text = json.dumps(events)
    assert _leaks(text, {SENTINEL, report["project"], "12.345678", "98.765432"}) == []


def test_the_sentinel_leaves_the_private_api_only_to_its_members(db: Session, canary: dict[str, Any]) -> None:
    """The canary is real: its owner reads it back, a stranger does not."""
    owner_tab = _tab(db, canary["owner"])
    detail = owner_tab.get(f"/v1/my/projects/{canary['project']}")
    assert SENTINEL in detail.text
    stranger = _tab(db, _user(db, "stranger@example.org"))
    assert stranger.get(f"/v1/my/projects/{canary['project']}").status_code == 404
    assert (
        _leaks(stranger.get("/v1/my/projects").text + stranger.get("/v1/my/pins").text, canary["secrets"])
        == []
    )
