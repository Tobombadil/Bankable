"""The 2026-09-30 content audit's social findings (F1-F3, F5, F6, F9, F11, F13-F15) and legal L-8,
L-9, L-12, as fixed on 2026-10-07. Each test names the finding it pins; every one of them fails on
the tree before the fix (af6f250).

Source fixtures use the real manifest names (`data/sources.yaml`) where a name is the point: F1 was
invisible to the old tests because no fixture source had a digit in its name.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.admin_posts import router as admin_posts_router
from services.api.app import app
from services.api.conftest import (
    make_event,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import Licence, Post, Source
from services.social import editorial
from services.social.editorial import CHANNEL_LIMITS, SocialEvent, channels_for_event
from services.social.publishers.bluesky import BlueskyPublisher
from services.social.worker import draft_posts_tick
from tests.conftest import login, make_account, make_user
from tests.social_support import seed_graduated_pair

UTC = dt.UTC
RETRIEVED_AT = dt.datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
NESO_CREDIT = "Supported by National Energy SO Open Data"
EIA_NAME = "EIA-860M Preliminary Monthly Electric Generator Inventory"

if admin_posts_router not in getattr(app, "_admin_posts_mounted", []):
    app.include_router(admin_posts_router)
    app._admin_posts_mounted = [*getattr(app, "_admin_posts_mounted", []), admin_posts_router]  # type: ignore[attr-defined]


def _eia_event(**overrides: object) -> SocialEvent:
    """Marici as the 2026-09-30 store holds it: EIA-860M, CAISO balancing area, 400 MW storage."""
    fields: dict[str, object] = {
        "event_id": "36",
        "event_type": "proposal.status_changed",
        "event_date": dt.date(2026, 9, 27),
        "subject_type": "proposal",
        "subject_id": "marici",
        "source_id": "us.eia.860m",
        "source_name": EIA_NAME,
        "source_url": "https://www.eia.gov/electricity/data/eia860m/",
        "retrieved_at": RETRIEVED_AT,
        "reuse_class": "open",
        "page_url": "https://infraque.com/proposals/marici-f2a8zc",
        "proposal_name": "Marici",
        "technology": "storage",
        "capacity_mw": 400.0,
        "county": "Los Angeles",
        "state": "CA",
        "iso_rto": "CAISO",
        "status_from": "permitted",
        "status_to": "under_construction",
        "source_category": "registry",
        "licence_name": f"{EIA_NAME} terms of use",
        "eia_plant_id": "69508",
        "kind": "storage",
    }
    fields.update(overrides)
    # Only the fields this tree's `SocialEvent` knows, so the file also runs against the tree before
    # the fix and fails there on behaviour rather than on a constructor argument.
    known = {f.name for f in dataclasses.fields(SocialEvent)}
    return SocialEvent(**{k: v for k, v in fields.items() if k in known})  # type: ignore[arg-type]


def _registry_source(
    db: Session, licence: Licence, *, id_: str = "us.eia.860m", name: str = EIA_NAME
) -> Source:
    src = make_public_source(db, licence, id_=id_)
    src.name = name
    src.category = "registry"
    db.flush()
    return src


# ============================================================================= F1: the number gate
@pytest.mark.parametrize(
    "source_name",
    [EIA_NAME, "Grants.gov Search2 API", "EU Tenders Electronic Daily (TED) API v3"],
)
def test_f1_digits_in_a_source_name_are_not_claimed_numbers(source_name: str) -> None:
    event = _eia_event(
        event_type="proposal.new", source_name=source_name, source_id="us.test", licence_name=None
    )
    channels = channels_for_event(event)
    assert channels
    for channel in channels:
        draft = editorial.build_draft(event, channel)
        assert draft.validation is not None
        assert draft.validation.passed, draft.validation.failures


def test_f1_an_invented_number_still_fails() -> None:
    event = _eia_event()
    body, *_ = editorial.render(event, "bluesky")
    result = editorial.validate_draft(
        body.replace("400 MW", "450 MW"),
        event,
        "bluesky",
        attribution_line=editorial.attribution_line_for(event),
        disclosure_text=editorial.disclosure_text_for("bluesky"),
        delayed_tier_notice=None,
    )
    assert not result.passed
    assert "numbers not traceable to event fields: ['450']" in result.failures


def test_f1_eia_events_draft_instead_of_erroring(db: Session, db_sessionmaker: sessionmaker[Session]) -> None:
    source = _registry_source(db, make_open_licence(db))
    proposal = make_visible_proposal(db, source, lifecycle_state="under_construction", technology="storage")
    proposal.capacity_mw = 400.0
    event = make_event(db, proposal, source, event_type="status_change")
    event.before = {"lifecycle_state": "permitted"}
    event.after = {"lifecycle_state": "under_construction"}
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.errors == ()
    assert report.posts_created == 3  # bluesky, x and (construction) linkedin
    assert report.posts_held == 0


def test_f1_a_draft_that_fails_a_gate_is_held_for_review_not_lost(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    source = make_public_source(db, make_open_licence(db))
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    proposal.name_canonical = "Massive Solar"  # a banned word (docs/32 §3.4) in a real name
    event = make_event(db, proposal, source, event_type="created")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.errors == ()
    assert report.posts_created == 2
    assert report.posts_held == 2
    posts = db.scalars(select(Post).where(Post.event_id == event.id)).all()
    assert {p.state for p in posts} == {"draft"}
    for post in posts:
        assert post.gate_failures and "banned words" in post.gate_failures[0]


# ============================================================================= F2: transitions
@pytest.mark.parametrize(
    ("before", "after"),
    # Every transition in the 2026-09-30 store (22 status_change events, all EIA-860M).
    [
        ("permitted", "under_construction"),
        ("announced", "under_construction"),
        ("announced", "filed"),
        ("filed", "under_construction"),
    ],
)
def test_f2_every_real_forward_transition_earns_a_post(before: str, after: str) -> None:
    event = _eia_event(status_from=before, status_to=after)
    assert "bluesky" in channels_for_event(event)


@pytest.mark.parametrize(
    ("before", "after"),
    [("under_construction", "permitted"), ("built", "filed"), ("filed", "unknown"), ("filed", "withdrawn")],
)
def test_f2_a_backward_or_off_ladder_move_is_not_news(before: str, after: str) -> None:
    assert channels_for_event(_eia_event(status_from=before, status_to=after)) == ()


# ============================================================================= F3: what the source is
def test_f3_an_inventory_row_never_claims_a_queue_or_its_balancing_area() -> None:
    event = _eia_event(
        event_type="proposal.new",
        status_from=None,
        iso_rto="TEPC",
        developer_org="Winchester Solar I, LLC",
    )
    for channel in channels_for_event(event):
        body = editorial.build_draft(event, channel).body
        assert "queue" not in body.lower()
        assert "TEPC" not in body
        assert "proposed" not in body
        assert "Developer" not in body
        # What the source is: the long phrase on LinkedIn; on the short formats "EIA-860M", with the
        # credit line spelling out "Preliminary Monthly Electric Generator Inventory".
        if channel == "linkedin":
            assert "Newly listed in EIA's monthly generator inventory (EIA-860M): Marici" in body
        else:
            assert body.startswith("Newly listed in EIA-860M: Marici, 400 MW storage")
        assert f"Source: {EIA_NAME}." in body
        assert "status: under construction" in body


def test_f3_a_queue_row_names_its_queue() -> None:
    event = _eia_event(
        event_type="proposal.new",
        source_id="us.iso.caiso.gen_queue",
        source_name="CAISO Public Queue Report",
        source_category="generation_queue",
        queue_id="1949",
        status_from=None,
        status_to="studied",
        eia_plant_id=None,
    )
    body = editorial.build_draft(event, "bluesky").body
    assert body.startswith("New in the CAISO queue: Marici, 400 MW storage")
    linkedin = editorial.build_draft(event, "linkedin").body
    assert linkedin.startswith("New in the CAISO interconnection queue: Marici, 400 MW storage")
    assert "Queue 1949." in body


# ============================================================================= credits (W3, L-10, L-12)
def test_credit_is_the_licence_credit_verbatim_with_neso_mandated_statement() -> None:
    event = _eia_event(
        source_id="gb.neso.tec_register",
        source_name="NESO Transmission Entry Capacity (TEC) Register",
        source_category="generation_queue",
        credit_line=NESO_CREDIT,
        country="GB",
        state=None,
        county="Devon",
        eia_plant_id=None,
        reuse_class="attribution",
    )
    channels = channels_for_event(event)
    assert channels
    for channel in channels:
        draft = editorial.build_draft(event, channel)
        assert draft.attribution_line == NESO_CREDIT
        assert NESO_CREDIT in draft.body
        assert "Source: NESO" not in draft.body
        assert draft.validation is not None and draft.validation.passed, draft.validation.failures


def test_credit_line_for_follows_the_record_page(db: Session) -> None:
    from services.social.db_events import credit_line_for

    licence = make_open_licence(db)
    licence.attribution_text = f"{NESO_CREDIT}. Capacity per stage derived by the platform."
    source = make_public_source(db, licence)
    assert credit_line_for(source) == licence.attribution_text  # verbatim, changes statement included
    licence.attribution_text = None
    assert credit_line_for(source) == f"Source: {source.name}"  # the page's prefix for a bare name
    source.attribution_text = "Source: Test Operator"
    assert credit_line_for(source) == "Source: Test Operator"  # never "Source: Source:"


def test_worker_posts_carry_the_verbatim_credit(db: Session, db_sessionmaker: sessionmaker[Session]) -> None:
    licence = make_open_licence(db)
    licence.reuse_class = "attribution"
    licence.attribution_text = NESO_CREDIT
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    make_event(db, proposal, source, event_type="created")
    db.commit()

    draft_posts_tick(db_sessionmaker)

    posts = db.scalars(select(Post)).all()
    assert posts
    for post in posts:
        assert post.credit_line == NESO_CREDIT
        assert NESO_CREDIT in post.body


# ============================================================================= F5: reviewer edits
def _operator(db: Session):
    account = make_account(db, entitlement="admin", name="Ops")
    return make_user(db, account, email="ops-gates@example.com", role="operator")


def _worker_post(db: Session, db_sessionmaker: sessionmaker[Session], *, name: str | None = None) -> Post:
    source = make_public_source(db, make_open_licence(db))
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    if name:
        proposal.name_canonical = name
    make_event(db, proposal, source, event_type="created")
    db.commit()
    draft_posts_tick(db_sessionmaker)
    post = db.scalar(select(Post).where(Post.channel == "x"))
    assert post is not None
    return post


def test_f5_a_reviewer_can_edit_a_worker_draft(client, db: Session, db_sessionmaker) -> None:
    post = _worker_post(db, db_sessionmaker)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    unchanged = client.patch(f"/admin/v1/posts/{post.public_id}", json={"body": post.body, "reason": "check"})
    assert unchanged.status_code == 200, unchanged.json()

    reworded = post.body.replace("New in an interconnection queue", "Newly queued")
    edited = client.patch(f"/admin/v1/posts/{post.public_id}", json={"body": reworded, "reason": "wording"})
    assert edited.status_code == 200, edited.json()
    assert edited.json()["data"]["body"] == reworded


def test_f5_an_edit_is_held_to_the_drafting_gates(client, db: Session, db_sessionmaker) -> None:
    post = _worker_post(db, db_sessionmaker)
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    invented = post.body.replace("150 MW", "175 MW")
    resp = client.patch(f"/admin/v1/posts/{post.public_id}", json={"body": invented, "reason": "r"})
    assert resp.status_code == 400
    assert "numbers not traceable" in resp.json()["detail"]


def test_f1_f5_a_held_draft_is_approved_only_after_an_edit_clears_it(
    client, db: Session, db_sessionmaker
) -> None:
    post = _worker_post(db, db_sessionmaker, name="Massive Solar")
    assert post.gate_failures
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    refused = client.post(f"/admin/v1/posts/{post.public_id}/approve")
    assert refused.status_code == 422
    assert refused.json()["code"] == "gate_unmet"

    still_banned = client.patch(f"/admin/v1/posts/{post.public_id}", json={"body": post.body, "reason": "r"})
    assert still_banned.status_code == 400

    fixed_body = post.body.replace("Massive Solar, ", "")
    fixed = client.patch(
        f"/admin/v1/posts/{post.public_id}", json={"body": fixed_body, "reason": "drop name"}
    )
    assert fixed.status_code == 200, fixed.json()
    assert fixed.json()["data"]["gate_failures"] == []
    approved = client.post(f"/admin/v1/posts/{post.public_id}/approve")
    assert approved.status_code == 200


# ============================================================================= F6, L-9: posture
def test_f6_the_disclosure_follows_the_noncommercial_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editorial, "POSTURE", "noncommercial")
    for channel in ("bluesky", "x"):
        text = editorial.disclosure_text_for(channel)
        assert "commercial in nature" not in text
        assert "noncommercial posture" in text
    monkeypatch.setattr(editorial, "POSTURE", "commercial")
    assert "commercial in nature" in editorial.disclosure_text_for("x")


def test_f6_noncommercial_rows_are_never_posted_under_either_posture() -> None:
    assert "noncommercial" not in editorial.SOCIAL_REUSE_CLASSES
    event = _eia_event(reuse_class="noncommercial")
    assert channels_for_event(event) == ()
    body, *_ = editorial.render(event, "bluesky")
    result = editorial.validate_draft(
        body,
        event,
        "bluesky",
        attribution_line=editorial.attribution_line_for(event),
        disclosure_text="d",
        delayed_tier_notice=None,
    )
    assert any("not publishable on social channels" in f for f in result.failures)


# ============================================================================= F11, L-8: auto-publish
def _owner(db: Session):
    account = make_account(db, entitlement="admin", name="Owner")
    return make_user(db, account, email="owner-gates@example.com", role="owner")


def test_f11_linkedin_can_never_be_switched_to_auto_publish(client, db: Session) -> None:
    owner = _owner(db)
    db.commit()
    login(client, db, owner)
    resp = client.put(
        "/admin/v1/channels/linkedin/auto-publish",
        json={
            "auto_publish": True,
            "disclosure_label_confirmed": True,
            "event_types": ["proposal.new"],
            "reason": "try",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "gate_unmet"


def test_f11_the_switch_refuses_a_pair_that_has_not_graduated(client, db: Session) -> None:
    owner = _owner(db)
    db.commit()
    login(client, db, owner)
    no_types = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={"auto_publish": True, "disclosure_label_confirmed": True, "reason": "go"},
    )
    assert no_types.status_code == 400
    ungraduated = client.put(
        "/admin/v1/channels/bluesky/auto-publish",
        json={
            "auto_publish": True,
            "disclosure_label_confirmed": True,
            "event_types": ["proposal.new"],
            "reason": "go",
        },
    )
    assert ungraduated.status_code == 422
    assert "0 of 200" in ungraduated.json()["detail"]


def test_f11_the_switch_accepts_a_graduated_pair(client, db: Session) -> None:
    source = make_public_source(db, make_open_licence(db))
    proposal = make_visible_proposal(db, source)
    seed_graduated_pair(db, make_event(db, proposal, source), channel="x", event_type="proposal.new")
    owner = _owner(db)
    db.commit()
    login(client, db, owner)
    resp = client.put(
        "/admin/v1/channels/x/auto-publish",
        json={
            "auto_publish": True,
            "disclosure_label_confirmed": True,
            "event_types": ["proposal.new"],
            "reason": "graduated",
        },
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["data"]["auto_publish_event_types"] == ["proposal.new"]


# ============================================================================= F9: one post per plant
def test_f9_generators_of_one_plant_make_one_post_sized_for_the_plant(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    source = _registry_source(db, make_open_licence(db))
    location = make_location(db, source, source.licence, county_name="Ochiltree", state_code="US-TX")
    events = []
    for i in range(1, 7):
        proposal = make_visible_proposal(
            db,
            source,
            public_id_suffix=str(i),
            lifecycle_state="under_construction",
            technology="gas_cc",
            location=location,
        )
        proposal.name_canonical = "Project Matador Gas Plant (PMG)"
        proposal.capacity_mw = 50.0
        proposal.kind = "generation"
        proposal.identifiers = {"eia_plant_id": "69799", "eia_generator_id": f"FBG0{i}"}
        event = make_event(db, proposal, source, event_type="status_change")
        event.before = {"lifecycle_state": "permitted"}
        event.after = {"lifecycle_state": "under_construction"}
        events.append(event)
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.errors == ()
    posts = db.scalars(select(Post)).all()
    assert sorted(p.channel for p in posts) == ["bluesky", "linkedin", "x"]  # 300 MW reaches LinkedIn
    for post in posts:
        assert "300 MW gas (combined cycle) across 6 generators" in post.body
        assert "50 MW" not in post.body
        if post.channel == "linkedin":  # an optional clause the short formats drop for length
            assert "EIA plant 69799" in post.body
    assert report.events_eligible == 6


# ============================================================================= F13: data centres
def test_f13_a_data_centre_with_no_mw_is_posted_saying_so() -> None:
    event = _eia_event(
        event_type="proposal.new",
        source_id="us.va.deq.data_center_air_sites",
        source_name="Virginia DEQ Air Sites (Daily) — sites DEQ flags as data centres",
        source_category="permit",
        credit_line="Source: Virginia Department of Environmental Quality",
        kind="load",
        technology="load",
        capacity_mw=None,
        proposal_name="Example Data Center",
        county="Loudoun County",
        state="VA",
        status_from=None,
        status_to="filed",
        eia_plant_id=None,
    )
    assert channels_for_event(event) == ("bluesky", "x")
    body = editorial.build_draft(event, "bluesky").body
    assert "Example Data Center, large load, no MW stated, Loudoun County, VA" in body
    assert "County County" not in body


def test_f13_a_sized_load_is_judged_against_the_load_bar() -> None:
    small = _eia_event(event_type="proposal.new", kind="load", technology="load", capacity_mw=60.0)
    big = dataclasses.replace(small, capacity_mw=150.0)
    assert channels_for_event(small) == ()
    assert "bluesky" in channels_for_event(big)


# ============================================================================= F14: words, not tokens
def test_f14_copy_uses_the_site_labels_and_one_county_suffix() -> None:
    event = _eia_event(technology="gas_cc", county="Loudoun County", state="VA")
    for channel in channels_for_event(event):
        body = editorial.build_draft(event, channel).body
        assert "gas_cc" not in body and "under_construction" not in body
        assert "gas (combined cycle)" in body
        assert "permitted → under construction" in body
        assert "County County" not in body
        assert "400 MW" in body  # status posts always state the size
    linkedin = editorial.build_draft(event, "linkedin").body
    assert f"licence: {EIA_NAME} terms of use" in linkedin
    assert "; open)" not in linkedin


def test_f14_f15_bluesky_card_has_a_clickable_tagged_link_and_a_real_title() -> None:
    event = _eia_event()
    draft = dataclasses.replace(editorial.build_draft(event, "bluesky"), status="approved")
    payload = BlueskyPublisher().publish(draft, dry_run=True).would_send
    facet = payload["facets"][0]
    uri = facet["features"][0]["uri"]
    assert uri == draft.link_url and "utm_source=bluesky" in uri
    start, end = facet["index"]["byteStart"], facet["index"]["byteEnd"]
    assert draft.body.encode()[start:end].decode() == event.page_url
    assert payload["embed"]["external"]["title"] == "Marici"
    assert payload["embed"]["external"]["title"] != event.event_type
    assert payload["validation"]["passed"], payload["validation"]


# ============================================================================= F15: UTM per channel
@pytest.mark.parametrize("channel", ["x", "linkedin"])
def test_f15_the_link_a_reader_clicks_carries_the_channel_utm(channel: str) -> None:
    event = _eia_event()
    draft = editorial.build_draft(event, channel)
    assert draft.link_url in draft.body
    assert (
        f"utm_source={channel}&utm_medium=social&utm_campaign=proposal.status_changed&utm_content=36"
        in draft.body
    )
    assert editorial.channel_length(draft.body, channel) <= CHANNEL_LIMITS[channel]


def test_f15_worker_posts_on_x_carry_the_tagged_link_in_the_body(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    post = _worker_post(db, db_sessionmaker)
    assert post.link_url in post.body
    assert "utm_source=x" in post.body


# ============================================================================= survivors
def test_a_merged_away_record_posts_about_its_survivor(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    source = make_public_source(db, make_open_licence(db))
    survivor = make_visible_proposal(db, source, public_id_suffix="1")
    survivor.capacity_mw = 1150.0
    survivor.name_canonical = "Darden"
    absorbed = make_visible_proposal(db, source, public_id_suffix="2")
    absorbed.capacity_mw = 342.7
    absorbed.merged_into_id = survivor.id
    absorbed.publish_state = "unpublished"
    make_event(db, absorbed, source, event_type="status_change")
    db.commit()

    draft_posts_tick(db_sessionmaker)

    posts = db.scalars(select(Post)).all()
    assert posts
    for post in posts:
        assert post.subject_id == survivor.id
        assert "1,150 MW" in post.body
        assert f"/proposals/{survivor.slug}" in post.body


# ============================================================================= F10: the events feed
def test_f10_event_feed_items_say_what_changed(client, db: Session) -> None:
    source = make_public_source(db, make_open_licence(db))
    proposal = make_visible_proposal(db, source)
    make_event(db, proposal, source, event_type="status_change")  # announced -> filed
    db.commit()
    items = client.get("/feeds/events.json").json()["items"]
    assert items
    title = items[0]["title"]
    assert title == f"{proposal.name_canonical}: status announced → filed"
    assert "status_change" not in title
