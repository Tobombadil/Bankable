"""My pipeline's pages (`web/my_projects.py`; docs/19 §6.2 as amended; docs/30 §11.4-§11.5): the
switch, sign-in, private headers and robots, the Track block on record pages, the Tracker and its
filter, the new-item form, the item tabs and their forms, and that a public page never varies with
whether anyone tracks its record.

Drives `services.api.app.app` in process through the site, as `web/test_alerts_pages.py` does:
users register and verify through the site's own forms, so every private read and write goes
through the API's one authorisation function.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.auth import ResendEmailAdapter
from services.api.auth_routes import get_email_port
from services.api.conftest import (
    make_asset,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from tests.web_alerts_support import memory_sessionmaker
from web.api_client import ApiClient
from web.app import app as web_app
from web.my_projects import changed_view, deadline_view, track_offer

ORIGIN = {"origin": "http://testserver"}
SITE_TOKEN = "web-my-projects-test-token"
PASSWORD = "correct horse battery"
UTC = dt.UTC
_ITEM_HREF = re.compile(r"/my/items/(prj_[0-9A-HJKMNP-TV-Z]{10,26})")


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    default_limiter.reset()
    monkeypatch.setenv("MY_PIPELINE", "on")
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-web-my-projects")


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    return memory_sessionmaker()


@pytest.fixture()
def world(factory: sessionmaker[Session]) -> dict[str, str]:
    """A public project, an open RFP due in 20 days and a retiring plant; their ids and slugs."""
    with factory() as session:
        licence = make_open_licence(session)
        source = make_public_source(session, licence)
        proposal = make_visible_proposal(session, source)
        opportunity = make_visible_opportunity(session, source)
        opportunity.due_at = dt.datetime.now(UTC) + dt.timedelta(days=20)
        plant = make_asset(session, source, licence, status="retiring", name="Old Coal Plant")
        session.commit()
        return {
            "proposal": proposal.public_id,
            "proposal_slug": proposal.slug,
            "opportunity": opportunity.public_id,
            "opportunity_slug": opportunity.slug,
            "plant": plant.public_id,
            "plant_slug": plant.slug,
        }


@pytest.fixture()
def web_client(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setenv("API_INTERNAL_TOKEN", SITE_TOKEN)
    api_app.dependency_overrides[get_db] = _override_get_db
    api_app.dependency_overrides[get_email_port] = ResendEmailAdapter
    web_app.state.api_client = ApiClient(
        TestClient(api_app, base_url="http://api-internal", headers={"X-Internal-Token": SITE_TOKEN})
    )
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


Browser = Callable[[str], TestClient]


@pytest.fixture()
def browser(web_client: TestClient) -> Iterator[Browser]:
    """A registered, verified visitor per address, each with their own cookie jar."""
    opened: list[TestClient] = []

    def _open(email: str) -> TestClient:
        tab = web_client if not opened else TestClient(web_app)
        opened.append(tab)
        resp = tab.post(
            "/register", data={"email": email, "password": PASSWORD, "next": "/account"}, headers=ORIGIN
        )
        assert resp.status_code == 200, resp.text
        resent = tab.post("/account/resend", headers=ORIGIN)
        token = resent.text.split("/verify?token=")[1].split('"')[0]
        assert tab.get(f"/verify?token={token}").status_code == 200
        return tab

    yield _open
    for tab in opened[1:]:
        tab.close()


def _track(tab: TestClient, record_type: str, public_id: str, kind: str = "", **fields: str) -> str:
    """Track from a record page, then submit the prefilled form; returns the new item's id."""
    form = tab.post(
        "/my/items/new",
        data={"record_type": record_type, "public_id": public_id, "kind": kind},
        headers=ORIGIN,
    )
    assert form.status_code == 200, form.text
    title = re.search(
        r'id="item-title" name="title" type="text" maxlength="200" required value="([^"]*)"', form.text
    )
    kind_value = re.search(r'name="kind" value="([a-z]+)"', form.text)
    relation = re.search(r'name="relation" value="([a-z_]+)"', form.text)
    assert title and kind_value and relation
    data = {
        "kind": kind_value.group(1),
        "title": title.group(1).replace("&#39;", "'"),
        "from_record_type": record_type,
        "from_public_id": public_id,
        "relation": relation.group(1),
        "point": "record",
        **fields,
    }
    created = tab.post("/my/items", data=data, headers=ORIGIN, follow_redirects=False)
    assert created.status_code == 303, created.text
    match = _ITEM_HREF.search(created.headers["location"])
    assert match
    return match.group(1)


def _blank(tab: TestClient, kind: str, title: str, **fields: str) -> str:
    created = tab.post(
        "/my/items", data={"kind": kind, "title": title, **fields}, headers=ORIGIN, follow_redirects=False
    )
    assert created.status_code == 303, created.text
    match = _ITEM_HREF.search(created.headers["location"])
    assert match
    return match.group(1)


# ----------------------------------------------------------------------- switch, sign-in, privacy
def test_every_page_is_off_until_the_switch_is_on(
    web_client: TestClient, browser: Browser, world: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tab = browser("owner@example.org")
    item = _blank(tab, "project", "Mine")
    monkeypatch.delenv("MY_PIPELINE")
    for path in (
        "/my",
        "/my/items/new",
        f"/my/items/{item}",
        f"/my/items/{item}/checklist",
        "/my/invitations/accept?t=x",
    ):
        assert tab.get(path).status_code == 404, path
    assert tab.post("/my/items", data={"title": "x"}, headers=ORIGIN).status_code == 404
    page = tab.get(f"/proposals/{world['proposal_slug']}").text
    assert "Track this" not in page and 'action="/my/items/new"' not in page


def _answer(resp: Any) -> tuple[int, bytes, list[tuple[str, str]]]:
    headers = sorted((k, v) for k, v in resp.headers.items() if k.lower() not in ("date", "x-request-id"))
    return resp.status_code, resp.content, headers


def test_while_off_every_route_is_the_404_an_unknown_address_gets(
    web_client: TestClient, browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off, nothing says the pages exist: the same status, bytes and headers as an address no route
    matches (no private headers, no title naming My pipeline), signed in or not, for every method a
    route takes and for malformed ids, and `robots.txt` does not name them."""
    tab = browser("owner@example.org")
    item = _blank(tab, "bid", "Mine")
    assert "Disallow: /my\n" in web_client.get("/robots.txt").text
    monkeypatch.setenv("MY_PIPELINE", "off")
    anonymous = TestClient(web_app)
    for client in (tab, anonymous):
        unknown = _answer(client.get("/no-such-page"))
        assert unknown[0] == 404
        for path in (
            "/my",
            "/my?type=bid",
            "/my/items/new",
            f"/my/items/{item}",
            f"/my/items/{item}/linked",
            f"/my/items/{item}/documents",
            "/my/items/not-an-id",
            "/my/invitations/accept?t=x",
        ):
            assert _answer(client.get(path)) == unknown, path
        unknown_post = _answer(client.post("/no-such-page", data={"title": "x"}, headers=ORIGIN))
        for path in (
            "/my/items",
            "/my/items/new",
            f"/my/items/{item}/stage",
            f"/my/items/{item}/parent",
            f"/my/items/{item}/links/not-a-row/remove",
            f"/my/items/{item}/notes/not-a-note/delete",
            "/my/invitations/accept",
        ):
            assert _answer(client.post(path, data={"title": "x"}, headers=ORIGIN)) == unknown_post, path
    robots = web_client.get("/robots.txt").text
    assert "/my" not in robots and "Disallow: /admin\n" in robots
    anonymous.close()


def test_a_signed_out_visitor_is_sent_to_sign_in_and_never_offered_track(
    web_client: TestClient, world: dict[str, str]
) -> None:
    resp = web_client.get("/my", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=%2Fmy"
    for path in (
        f"/proposals/{world['proposal_slug']}",
        f"/opportunities/{world['opportunity_slug']}",
        f"/assets/{world['plant_slug']}",
    ):
        page = web_client.get(path)
        assert page.status_code == 200, path
        assert "Track this" not in page.text and 'href="/my"' not in page.text, path
        assert "Start a deal" not in page.text


def test_private_pages_are_no_store_noindex_no_referrer_and_name_no_item(
    web_client: TestClient, browser: Browser
) -> None:
    tab = browser("owner@example.org")
    item = _blank(tab, "bid", "QZV secret bid title")
    for path in (
        "/my",
        "/my/items/new",
        f"/my/items/{item}",
        f"/my/items/{item}/linked",
        f"/my/items/{item}/checklist",
        f"/my/items/{item}/notes",
        f"/my/items/{item}/people",
        "/my/items/prj_0000000000000000000000000",
    ):
        resp = tab.get(path)
        assert resp.status_code in (200, 404), path
        assert resp.headers["cache-control"] == "private, no-store", path
        assert resp.headers["x-robots-tag"] == "noindex, nofollow", path
        assert resp.headers["referrer-policy"] == "no-referrer", path
        assert "cookie" in resp.headers["vary"].lower(), path
        assert '<meta name="robots" content="noindex, nofollow">' in resp.text, path
        head = resp.text.split("</head>", 1)[0]
        assert "QZV" not in head, path
    robots = web_client.get("/robots.txt").text
    assert "Disallow: /my\n" in robots and "Disallow: /my/\n" in robots


def test_a_form_posted_under_no_referrer_passes_on_the_browsers_own_word(browser: Browser) -> None:
    """Under `Referrer-Policy: no-referrer` a browser posts with `Origin: null` and no `Referer`;
    `Sec-Fetch-Site: same-origin` (which no page script can set) is then the check, and anything
    else fails closed."""
    tab = browser("owner@example.org")
    item = _blank(tab, "project", "Mine")
    path = f"/my/items/{item}/stage"
    same_site = {"origin": "null", "sec-fetch-site": "same-origin"}
    assert (
        tab.post(path, data={"stage": "development"}, headers=same_site, follow_redirects=False).status_code
        == 303
    )
    for headers in (
        {"origin": "null"},
        {"origin": "null", "sec-fetch-site": "cross-site"},
        {"origin": "null", "sec-fetch-site": "same-site"},
        {"origin": "https://elsewhere.example", "sec-fetch-site": "same-origin"},
        {"sec-fetch-site": "none"},
    ):
        assert tab.post(path, data={"stage": "prospecting"}, headers=headers).status_code == 403, headers


def test_forms_need_the_same_origin(browser: Browser) -> None:
    tab = browser("owner@example.org")
    item = _blank(tab, "project", "Mine")
    assert tab.post("/my/items", data={"title": "x"}).status_code == 403
    assert tab.post(f"/my/items/{item}/stage", data={"stage": "development"}).status_code == 403
    assert (
        tab.post(
            "/my/items/new", data={"record_type": "proposal", "public_id": "prop_0000000000"}
        ).status_code
        == 403
    )


def test_someone_elses_item_and_an_unknown_tab_are_the_same_404(browser: Browser) -> None:
    owner, outsider = browser("owner@example.org"), browser("outsider@example.org")
    item = _blank(owner, "project", "Mine")
    for path in (f"/my/items/{item}", f"/my/items/{item}/people", "/my/items/prj_0000000000000000000000000"):
        assert outsider.get(path).status_code == 404, path
    assert owner.get(f"/my/items/{item}/documents").status_code == 404
    assert owner.get("/my/items/not-an-id").status_code == 404


# ------------------------------------------------------------------------------- Track, tracker
def test_track_offers_the_kinds_each_record_makes() -> None:
    request: Any = SimpleNamespace(cookies={"session": "x"})
    assert (
        track_offer(request, "opportunity", {"public_id": "opp_1", "status": "open"})["choices"][0][0]
        == "bid"
    )
    assert track_offer(request, "opportunity", {"public_id": "opp_1", "status": "awarded"}) is None
    proposal = track_offer(request, "proposal", {"public_id": "prop_1", "lifecycle_state": "filed"})
    assert [kind for kind, _label in proposal["choices"]] == ["project", "investment"]
    assert track_offer(request, "proposal", {"public_id": "prop_1", "lifecycle_state": "withdrawn"}) is None
    plant = {"public_id": "asset_1", "asset_type": "power_plant", "status": "retiring"}
    assert track_offer(request, "asset", plant)["choices"][0][0] == "project"
    assert track_offer(request, "asset", {**plant, "status": "operating"}) is None
    assert track_offer(request, "organization", {"public_id": "org_1"}) is None
    request.cookies = {}
    assert track_offer(request, "opportunity", {"public_id": "opp_1", "status": "open"}) is None


def test_track_on_an_rfp_makes_a_bid_and_the_tracker_lists_it_by_deadline(
    browser: Browser, world: dict[str, str]
) -> None:
    tab = browser("owner@example.org")
    page = tab.get(f"/opportunities/{world['opportunity_slug']}").text
    assert "Track this" in page and 'name="kind" value="bid"' in page
    assert "Add it to your private tracker: its deadline and changes follow the public record." in page
    assert "Only you, and the people you invite, can see it." in page
    form = tab.post(
        "/my/items/new",
        data={"record_type": "opportunity", "public_id": world["opportunity"]},
        headers=ORIGIN,
    )
    assert (
        "Bid: Test RFP 1" in form.text
        and "Considering" in form.text
        and "For which of your projects" in form.text
    )
    bid = _track(tab, "opportunity", world["opportunity"])
    _blank(tab, "investment", "A fund")
    tracker = tab.get("/my")
    assert tracker.status_code == 200
    text = tracker.text
    assert text.index("Bid: Test RFP 1") < text.index("A fund"), "the dated item comes first"
    assert "in 20 days" in text and "0 of 8" in text
    assert f'href="/my/items/{bid}"' in text
    assert 'aria-current="page">All <span class="tnum">2</span>' in text
    only_bids = tab.get("/my?type=bid").text
    assert "Bid: Test RFP 1" in only_bids and "A fund" not in only_bids
    assert 'Bids <span class="tnum">1</span>' in only_bids and "Only you see these counts." in only_bids


def test_track_on_a_project_page_offers_project_or_investment(
    browser: Browser, world: dict[str, str]
) -> None:
    tab = browser("owner@example.org")
    page = tab.get(f"/proposals/{world['proposal_slug']}").text
    assert 'value="project" checked' in page and 'value="investment"' in page
    investment = _track(tab, "proposal", world["proposal"], kind="investment")
    overview = tab.get(f"/my/items/{investment}").text
    assert "Investment" in overview and "Screening" in overview and "Term sheet" in overview
    plant = tab.get(f"/assets/{world['plant_slug']}").text
    assert "Track this" in plant
    project = _track(tab, "asset", world["plant"])
    assert "Prospecting" in tab.get(f"/my/items/{project}").text


def test_the_empty_tracker_points_to_the_map_the_site_finder_and_rfps(browser: Browser) -> None:
    text = browser("owner@example.org").get("/my").text
    assert "Nothing tracked yet" in text
    for href in ('href="/"', 'href="/site-finder"', 'href="/opportunities"'):
        assert href in text, href


def test_the_new_item_form_asks_the_type_first(browser: Browser) -> None:
    tab = browser("owner@example.org")
    project = tab.get("/my/items/new").text
    assert (
        'href="/my/items/new?type=bid"' in project
        and "Prospecting" in project
        and "Considering" not in project
    )
    bid = tab.get("/my/items/new?type=bid").text
    assert "Considering" in bid and "Withdrawn" in bid and "Submitted on" in bid
    refused = tab.post(
        "/my/items", data={"kind": "bid", "title": "x", "stage": "development"}, headers=ORIGIN
    )
    assert refused.status_code == 400 and "a bid&#39;s stage is one of" in refused.text


# ------------------------------------------------------------------------------- the item tabs
def test_the_item_tabs_and_their_primary_actions(browser: Browser, world: dict[str, str]) -> None:
    tab = browser("owner@example.org")
    project = _blank(tab, "project", "North Ridge")
    bid = _track(tab, "opportunity", world["opportunity"])
    overview = tab.get(f"/my/items/{project}").text
    for label in ("Overview", "Linked records", "Checklist", "Notes", "People"):
        assert f">{label}" in overview, label
    assert "Documents" not in overview
    assert f'href="/my/items/{project}" aria-current="page"' in overview
    assert "Coming soon: no email is sent yet." in overview and "Your status, not verified." in overview

    staged = tab.post(
        f"/my/items/{project}/stage", data={"stage": "development"}, headers=ORIGIN, follow_redirects=False
    )
    assert staged.status_code == 303
    assert "Stage: Development" in tab.get(staged.headers["location"]).text
    bad = tab.post(f"/my/items/{project}/stage", data={"stage": "won"}, headers=ORIGIN)
    assert bad.status_code == 400 and "a project&#39;s stage is one of" in bad.text

    attached = tab.post(
        f"/my/items/{bid}/parent", data={"parent_project_id": project}, headers=ORIGIN, follow_redirects=False
    )
    assert attached.status_code == 303
    assert "North Ridge" in tab.get(f"/my/items/{bid}").text
    assert "via its bid" in tab.get("/my").text
    opted = tab.post(
        f"/my/items/{project}/email-changes",
        data={"email_changes": "on"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert "none is sent yet" in tab.get(opted.headers["location"]).text

    linked = tab.post(
        f"/my/items/{project}/links",
        data={
            "reference": f"http://testserver/proposals/{world['proposal_slug']}",
            "relation": "this_project",
        },
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert linked.status_code == 303, linked.text
    records = tab.get(f"/my/items/{project}/linked").text
    assert "Test Storage Project 1" in records and "Sources:" in records
    unknown = tab.post(f"/my/items/{project}/links", data={"reference": "nonsense"}, headers=ORIGIN)
    assert unknown.status_code == 400 and "No record found" in unknown.text

    checklist = tab.get(f"/my/items/{bid}/checklist").text
    assert "Notice of intent or registration" in checklist and "Submission confirmation" in checklist
    item_id = re.search(r'name="was" value="([0-9a-f-]{36}):not_started"', checklist)
    assert item_id
    saved = tab.post(
        f"/my/items/{bid}/checklist",
        data={"status": [f"{item_id.group(1)}:done"], "was": [f"{item_id.group(1)}:not_started"]},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert f'value="{item_id.group(1)}:done" selected' in tab.get(f"/my/items/{bid}/checklist").text

    noted = tab.post(
        f"/my/items/{bid}/notes",
        data={"body": "Call the utility, see https://example.org"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert noted.status_code == 303
    notes = tab.get(f"/my/items/{bid}/notes").text
    assert 'href="https://example.org" rel="noopener noreferrer"' in notes

    invited = tab.post(
        f"/my/items/{bid}/invitations",
        data={"email": "partner@example.org", "role": "editor"},
        headers=ORIGIN,
    )
    assert invited.status_code == 200 and "/my/invitations/accept?t=" in invited.text
    assert "/my/invitations/accept?t=" not in tab.get(f"/my/items/{bid}/people").text, "shown once"
    assert "partner@example.org" in tab.get(f"/my/items/{bid}/people").text


def _invite(owner: TestClient, item: str, invitee: TestClient, email: str, role: str) -> None:
    invited = owner.post(f"/my/items/{item}/invitations", data={"email": email, "role": role}, headers=ORIGIN)
    token = re.search(r"/my/invitations/accept\?t=([A-Za-z0-9_-]+)", invited.text)
    assert token, invited.text
    joined = invitee.post(
        "/my/invitations/accept", data={"token": token.group(1)}, headers=ORIGIN, follow_redirects=False
    )
    assert joined.status_code == 303, joined.text


def test_saving_the_attach_form_unchanged_never_detaches_a_project_you_cannot_see(
    browser: Browser,
) -> None:
    """A collaborator on a bid who is not a member of its project sees no project, and is not told
    there is one; the form's first choice then is "Leave it as it is", which writes nothing. Only
    choosing "None: it stands alone" detaches it."""
    owner, partner = browser("owner@example.org"), browser("partner@example.org")
    project = _blank(owner, "project", "QZV hidden project")
    bid = _blank(owner, "bid", "Shared bid", parent_project_id=project)
    _invite(owner, bid, partner, "partner@example.org", "editor")
    overview = partner.get(f"/my/items/{bid}").text
    assert "QZV" not in overview and project not in overview
    assert "None of your projects" in overview
    assert '<option value="" selected>Leave it as it is</option>' in overview
    kept = partner.post(
        f"/my/items/{bid}/parent", data={"parent_project_id": ""}, headers=ORIGIN, follow_redirects=False
    )
    assert kept.status_code == 303 and kept.headers["location"].endswith("?done=unchanged")
    assert "Nothing changed." in partner.get(kept.headers["location"]).text
    assert "QZV hidden project" in owner.get(f"/my/items/{bid}").text, "still attached"
    shown = owner.get(f"/my/items/{bid}").text
    assert '<option value="" selected>QZV hidden project</option>' in shown
    # The project archived: saving the bid's form unchanged is still no write, never the API's refusal.
    owner.post(f"/my/items/{project}/archive", data={"archived": "1"}, headers=ORIGIN)
    assert (
        'QZV hidden project</a> <span class="my-shared">Archived</span>' in owner.get(f"/my/items/{bid}").text
    )
    unchanged = owner.post(
        f"/my/items/{bid}/parent", data={"parent_project_id": ""}, headers=ORIGIN, follow_redirects=False
    )
    assert unchanged.headers["location"].endswith("?done=unchanged")
    detached = partner.post(
        f"/my/items/{bid}/parent", data={"parent_project_id": "none"}, headers=ORIGIN, follow_redirects=False
    )
    assert detached.status_code == 303 and detached.headers["location"].endswith("?done=saved")
    assert "None of your projects" in owner.get(f"/my/items/{bid}").text
    cross = partner.post(f"/my/items/{bid}/parent", data={"parent_project_id": ""})
    assert cross.status_code == 403, "the no-op still checks the form's origin"


def test_an_archived_bid_is_marked_on_its_project_page(browser: Browser, world: dict[str, str]) -> None:
    tab = browser("owner@example.org")
    project = _blank(tab, "project", "North Ridge")
    bid = _track(tab, "opportunity", world["opportunity"], parent_project_id=project)
    assert "Via its bid" in tab.get(f"/my/items/{project}").text and "via its bid" in tab.get("/my").text
    archived = tab.post(
        f"/my/items/{bid}/archive", data={"archived": "1"}, headers=ORIGIN, follow_redirects=False
    )
    assert archived.status_code == 303
    page = tab.get(f"/my/items/{project}").text
    bids = page.split('id="bids-heading"', 1)[1].split("</section>", 1)[0]
    assert "Bid: Test RFP 1" in bids and "Archived" in bids and "due <time" not in bids
    assert "its deadline is not this project's" in bids
    assert "Via its bid" not in page and "None yet. Link an RFP" in page
    assert "via its bid" not in tab.get("/my").text


def test_an_invitation_is_accepted_by_its_address_only(browser: Browser) -> None:
    owner = browser("owner@example.org")
    item = _blank(owner, "investment", "Private fund")
    invited = owner.post(
        f"/my/items/{item}/invitations",
        data={"email": "partner@example.org", "role": "viewer"},
        headers=ORIGIN,
    )
    token = re.search(r"/my/invitations/accept\?t=([A-Za-z0-9_-]+)", invited.text)
    assert token
    stranger = browser("stranger@example.org")
    refused = stranger.get(f"/my/invitations/accept?t={token.group(1)}")
    assert "Private fund" not in refused.text and refused.status_code >= 400
    partner = browser("partner@example.org")
    preview = partner.get(f"/my/invitations/accept?t={token.group(1)}")
    assert preview.status_code == 200 and "Private fund" in preview.text and "a viewer" in preview.text
    assert preview.headers["referrer-policy"] == "no-referrer"
    joined = partner.post(
        "/my/invitations/accept", data={"token": token.group(1)}, headers=ORIGIN, follow_redirects=False
    )
    assert joined.status_code == 303 and item in joined.headers["location"]
    assert "Private fund" in partner.get("/my").text


# ---------------------------------------------------------------- public pages never vary with tracking
def test_a_public_record_page_is_byte_identical_whether_or_not_anyone_tracks_it(
    web_client: TestClient, browser: Browser, world: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision row 2026-10-23: no page ever shows how many users track a record. The record pages
    and the record's API answers are the same bytes before and after two users track it, for a
    signed-out visitor and for a third, signed-in one."""
    monkeypatch.setattr("services.api.serialize.utcnow", lambda: dt.datetime(2026, 10, 23, 12, 0, tzinfo=UTC))
    monkeypatch.setattr("services.api.request_context.new_request_id", lambda: "req_fixed0000")
    anonymous = TestClient(web_app)
    reader = browser("reader@example.org")
    pages = (
        f"/proposals/{world['proposal_slug']}",
        f"/opportunities/{world['opportunity_slug']}",
        f"/assets/{world['plant_slug']}",
        "/proposals",
        "/opportunities",
    )
    api = TestClient(api_app)
    records = (
        f"/v1/proposals/{world['proposal']}",
        f"/v1/opportunities/{world['opportunity']}",
        f"/v1/assets/{world['plant']}",
    )

    def snapshot() -> list[bytes]:
        out = [client.get(path).content for path in pages for client in (anonymous, reader)]
        out += [api.get(path).content for path in records]
        return out

    before = snapshot()
    for email in ("first@example.org", "second@example.org"):
        tab = browser(email)
        _track(tab, "proposal", world["proposal"])
        _track(tab, "proposal", world["proposal"], kind="investment")
        _track(tab, "opportunity", world["opportunity"])
        _track(tab, "asset", world["plant"])
    after = snapshot()
    assert after == before
    anonymous.close()


# ------------------------------------------------------------------------------------- the words
def test_the_badge_and_deadline_words() -> None:
    assert deadline_view(None) is None
    view = deadline_view({"due_at": "2026-12-15T17:00:00Z", "days_left": 53, "record": {}, "via_bid": None})
    assert view is not None and (view["date"], view["when"]) == ("15 Dec", "in 53 days")
    soon = deadline_view(
        {"due_at": "2026-10-24T00:00:00Z", "days_left": 1, "record": {}, "via_bid": {"public_id": "prj_1"}}
    )
    assert soon is not None and soon["when"] == "tomorrow" and soon["via_bid"]
    assert changed_view({"changed": False}) is None
    moved = changed_view(
        {
            "changed": True,
            "reason": "deadline",
            "text": "Deadline moved",
            "count": 1,
            "at": "2026-10-20T00:00:00Z",
        }
    )
    assert moved == {"reason": "deadline", "text": "Deadline moved", "date": "20 Oct"}
    many = changed_view(
        {"changed": True, "reason": "status", "text": "Status changed", "count": 2, "at": None}
    )
    assert many is not None and many["text"] == "2 status changes"
