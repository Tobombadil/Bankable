"""An organisation taken down through the admin route disappears from every public read
(docs/40-launch-runbook.md §6 item 2; migration 0022; `services/api/visibility.py`'s organisation
arm) and comes back on republish.

The state is written on the row here, as the admin route writes it; the route itself (reason,
audit event, takedown flag, the ungated admin read) is `tests/test_api_admin_records.py`'s, and the
whole loop -- panel takedown, public pages, sitemap, search, republish -- is
`web/test_admin_records.py`'s. This file reads the public surfaces a reader or a crawler would use:
the organisation routes, the `sponsor`/`issuer` embeds, the asset owners table and the filters that
match through an organisation, the ownership tree in both directions, and the social-post bridge.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import services.api.visibility as visibility
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_event,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Organization
from services.social.db_events import social_event_from_db


def _set_state(db: Session, org: Organization, state: str) -> None:
    org.publish_state = state
    db.commit()


def _problem_shape(body: dict[str, Any]) -> dict[str, Any]:
    """The 404 problem minus the two fields that differ per request by construction."""
    return {k: v for k, v in body.items() if k not in {"instance", "request_id"}}


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """One organisation that sponsors a proposal, issues an opportunity, owns an asset and sits
    between a visible parent and a visible child -- every edge the takedown has to cut."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    parent = make_org(db, "Visible Parent Holdings")
    target = make_org(db, "Takedown Target Energy")
    child = make_org(db, "Visible Child Operating")
    target.parent_org_id = parent.id
    child.parent_org_id = target.id
    loc = make_location(db, src, lic, geom=(-98.0, 32.05), precision="exact")
    proposal = make_visible_proposal(db, src, public_id_suffix="1", sponsor=target, location=loc)
    opportunity = make_visible_opportunity(db, src, public_id_suffix="1")
    opportunity.issuer_org_id = target.id
    asset = make_asset(db, src, lic, source_asset_id="a1", name="Shared Plant", geom=(-98.0, 32.0))
    make_asset_owner(db, asset, target, src, lic, role="owner", share_pct=60.0)
    make_asset_owner(db, asset, parent, src, lic, role="operator", share_pct=None)
    child_asset = make_asset(db, src, lic, source_asset_id="a2", name="Child Plant", geom=(-98.1, 32.0))
    make_asset_owner(db, child_asset, child, src, lic, role="owner", share_pct=100.0)
    db.commit()
    return {
        "src": src,
        "parent": parent,
        "target": target,
        "child": child,
        "proposal": proposal,
        "opportunity": opportunity,
        "asset": asset,
    }


# ------------------------------------------------------------------------ the predicate itself
def test_the_organisation_arm_is_the_state_alone_and_the_same_on_every_tier(db: Session) -> None:
    org = make_org(db)
    assert visibility.organization_visible(org)
    for state in ("pending_review", "ingest_only", "api_only", "unpublished"):
        org.publish_state = state
        assert not visibility.organization_visible(org), state
    rendered = {
        tier: [
            str(c.compile(compile_kwargs={"literal_binds": True}))
            for c in visibility.organization_visibility_filter(tier)
        ]
        for tier in ("public", "pro", "api")
    }
    assert rendered["public"] == rendered["pro"] == rendered["api"]
    assert rendered["public"] == ["organization.publish_state = 'public'"]


# ----------------------------------------------------------------------- organisation routes
def test_a_taken_down_organisation_is_404_everywhere_with_the_unknown_id_problem(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    target = world["target"]
    unknown = client.get("/v1/organizations/org_0000000000")
    assert unknown.status_code == 404

    _set_state(db, target, "unpublished")

    base = f"/v1/organizations/{target.public_id}"
    for path in (
        base,
        f"{base}/proposals",
        f"{base}/opportunities",
        f"{base}/assets",
        f"{base}/nearby-proposals",
    ):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert _problem_shape(resp.json()) == _problem_shape(unknown.json()), path

    listed = client.get("/v1/organizations", params={"limit": 100}).json()["data"]
    assert target.public_id not in {o["public_id"] for o in listed}
    assert client.get("/v1/organizations", params={"slug": target.slug}).json()["data"] == []
    assert client.get("/v1/organizations", params={"q": "takedown target"}).json()["data"] == []


def test_every_non_public_state_hides_it_not_only_unpublished(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    target = world["target"]
    for state in ("pending_review", "ingest_only", "api_only"):
        _set_state(db, target, state)
        assert client.get(f"/v1/organizations/{target.public_id}").status_code == 404, state
    _set_state(db, target, "public")
    assert client.get(f"/v1/organizations/{target.public_id}").status_code == 200


# ---------------------------------------------------------------------- embeds and filters
def test_the_sponsor_and_issuer_embeds_become_null_and_the_record_stays(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    proposal, opportunity, target = world["proposal"], world["opportunity"], world["target"]
    assert (
        client.get(f"/v1/proposals/{proposal.public_id}").json()["data"]["sponsor"]["public_id"]
        == target.public_id
    )

    _set_state(db, target, "unpublished")

    detail = client.get(f"/v1/proposals/{proposal.public_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["sponsor"] is None
    listed = client.get("/v1/proposals").json()["data"]
    assert [p["sponsor"] for p in listed if p["public_id"] == proposal.public_id] == [None]
    opp = client.get(f"/v1/opportunities/{opportunity.public_id}")
    assert opp.status_code == 200
    assert opp.json()["data"]["issuer"] is None
    # `q=` matching the hidden sponsor's name would confirm the name behind the `null`.
    assert client.get("/v1/proposals", params={"q": "takedown target"}).json()["data"] == []
    assert client.get("/v1/opportunities", params={"q": "takedown target"}).json()["data"] == []
    assert "Takedown Target" not in client.get(f"/v1/proposals/{proposal.public_id}").text


def test_the_asset_owner_edge_is_omitted_and_the_filters_match_nothing(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    asset, target, parent = world["asset"], world["target"], world["parent"]
    before = client.get(f"/v1/assets/{asset.public_id}").json()["data"]["owners"]
    assert {o["organization"]["public_id"] for o in before} == {target.public_id, parent.public_id}
    assert [
        a["public_id"]
        for a in client.get("/v1/assets", params={"organization": target.public_id}).json()["data"]
    ] == [asset.public_id]

    _set_state(db, target, "unpublished")

    detail = client.get(f"/v1/assets/{asset.public_id}")
    assert detail.status_code == 200
    owners = detail.json()["data"]["owners"]
    assert [o["organization"]["public_id"] for o in owners] == [parent.public_id]
    assert "Takedown Target" not in detail.text
    assert client.get("/v1/assets", params={"organization": target.public_id}).json()["data"] == []
    assert client.get("/v1/assets", params={"q": "takedown target"}).json()["data"] == []
    geo = client.get("/v1/assets/geo", params={"organization": target.public_id})
    assert geo.status_code == 200
    assert geo.json()["data"]["features"] == []


def test_the_ownership_tree_is_cut_at_the_hidden_organisation_in_both_directions(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    parent, target, child = world["parent"], world["target"], world["child"]
    before = client.get(f"/v1/organizations/{child.public_id}").json()["data"]
    assert before["parent"]["public_id"] == target.public_id
    assert [a["organization"]["public_id"] for a in before["ancestors"]] == [
        parent.public_id,
        target.public_id,
    ]

    _set_state(db, target, "unpublished")

    below = client.get(f"/v1/organizations/{child.public_id}")
    assert below.status_code == 200
    data = below.json()["data"]
    assert data["parent"] is None and data["parent_edge"] is None and data["ancestors"] == []
    assert "Takedown Target" not in below.text

    above = client.get(f"/v1/organizations/{parent.public_id}").json()["data"]
    assert above["subsidiaries"] == [] and above["subsidiary_count"] == 0 and above["descendant_count"] == 0
    # The walk does not pass through the hidden organisation to its child's assets either.
    group = client.get(f"/v1/organizations/{parent.public_id}/assets", params={"scope": "all"}).json()
    assert group["scope"]["organizations"] == 1
    assert {r["name"] for r in group["data"]} == {"Shared Plant"}  # the parent's own operator edge
    proposals = client.get(f"/v1/organizations/{parent.public_id}/proposals", params={"scope": "all"}).json()
    assert proposals["data"] == []


def test_republish_restores_every_surface(client: TestClient, db: Session, world: dict[str, Any]) -> None:
    target, child, proposal, asset = world["target"], world["child"], world["proposal"], world["asset"]
    _set_state(db, target, "unpublished")
    _set_state(db, target, "public")

    assert client.get(f"/v1/organizations/{target.public_id}").status_code == 200
    assert (
        client.get(f"/v1/proposals/{proposal.public_id}").json()["data"]["sponsor"]["public_id"]
        == target.public_id
    )
    owners = client.get(f"/v1/assets/{asset.public_id}").json()["data"]["owners"]
    assert target.public_id in {o["organization"]["public_id"] for o in owners}
    assert (
        client.get(f"/v1/organizations/{child.public_id}").json()["data"]["parent"]["public_id"]
        == target.public_id
    )


# ------------------------------------------------------------------------------ social bridge
def test_a_post_draft_does_not_name_a_hidden_sponsor_or_issuer(db: Session, world: dict[str, Any]) -> None:
    target, proposal, opportunity, src = (
        world["target"],
        world["proposal"],
        world["opportunity"],
        world["src"],
    )
    prop_event = make_event(db, proposal, src, event_type="created")
    opp_event = make_event(db, proposal, src, event_type="opened")
    opp_event.subject_type, opp_event.subject_id = "opportunity", opportunity.id
    opp_event.idempotency_key = f"{src.id}:{opportunity.public_id}:opened"
    db.commit()
    visible = social_event_from_db(db, prop_event)
    assert visible is not None and visible.developer_org == "Takedown Target Energy"

    target.publish_state = "unpublished"
    db.commit()
    hidden = social_event_from_db(db, prop_event)
    assert hidden is not None and hidden.developer_org is None
    # An RFP post prints the issuer unconditionally, so with the issuer hidden it is not drafted.
    assert social_event_from_db(db, opp_event) is None
