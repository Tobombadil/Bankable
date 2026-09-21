"""`services/api/orgtree.py` and the scoped organisation endpoints.

**What is real here and what is a fixture.** The loaded data has one genuinely interesting shape
and it is shallow: 298 parent links, of which 289 come from GLEIF Level 2 and 9 from the curated
file, and the deepest real chain is three levels (THE SOUTHERN COMPANY -> Southern Company Gas ->
its gas utilities). The owner's reference case, Tallgrass Energy over nine operating subsidiaries,
is **one level**. So every test below that exercises depth beyond two levels, the depth cap, the
organisation cap or a cycle builds its own organisations: those states are reachable by a loader
and are not reachable by a query against today's rows. That is stated here rather than left for a
reader to infer from the fixtures, because "tested" and "seen in production data" are different
claims and only the first one is true of most of this file.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from fastapi import Request

from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)
from services.api.errors import ProblemError
from services.api.orgtree import (
    MAX_DEPTH,
    MAX_SCOPE_ORGS,
    org_ancestors,
    org_scope,
    scope_from_request,
)
from services.db.models import Organization


def _chain(db, names: list[str]) -> list[Organization]:
    """A -> B -> C ... each the parent of the next."""
    orgs = [make_org(db, n) for n in names]
    for parent, child in itertools.pairwise(orgs):
        child.parent_org_id = parent.id
    db.flush()
    return orgs


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/organizations/org_x/assets",
            "query_string": query.encode(),
            "headers": [],
        }
    )


# ------------------------------------------------------------------ the walk down
def test_scope_all_descends_every_level_where_children_stops_at_one(db):
    root, mid, leaf = _chain(db, ["Fund Holdings", "Midco Energy", "Trailblazer Test Pipeline"])
    sibling = make_org(db, "Sibling Midco")
    sibling.parent_org_id = root.id
    db.commit()

    assert org_scope(db, root, "self").ids == [root.id]

    children = org_scope(db, root, "children")
    assert set(children.ids) == {root.id, mid.id, sibling.id}
    assert children.depth == 1 and children.depth_capped is False

    everything = org_scope(db, root, "all")
    assert set(everything.ids) == {root.id, mid.id, sibling.id, leaf.id}
    assert everything.depth == 2
    assert everything.as_meta()["organizations"] == 4


def test_a_leaf_reports_depth_zero_not_a_cap(db):
    org = make_org(db, "Lonely Co")
    db.commit()
    scope = org_scope(db, org, "all")
    assert scope.ids == [org.id]
    assert (scope.depth, scope.depth_capped, scope.truncated) == (0, False, False)


# ------------------------------------------------------------------ cycles
def test_a_two_node_cycle_terminates_and_is_reported(db):
    """Two loaders that cannot see each other's rows each name the other organisation as parent.
    Without the visited set this walk never returns."""
    a, b = _chain(db, ["Circular A", "Circular B"])
    a.parent_org_id = b.id
    db.commit()

    scope = org_scope(db, a, "all")
    assert set(scope.ids) == {a.id, b.id}
    assert scope.cycle_detected is True
    # And upward, from either end.
    assert [e.parent.id for e in org_ancestors(db, a)] == [b.id]
    assert [e.parent.id for e in org_ancestors(db, b)] == [a.id]


def test_a_self_parent_row_does_not_loop(db):
    org = make_org(db, "Ouroboros Energy")
    db.flush()
    org.parent_org_id = org.id
    db.commit()

    scope = org_scope(db, org, "all")
    assert scope.ids == [org.id] and scope.cycle_detected is True
    assert org_ancestors(db, org) == []


def test_a_diamond_counts_each_organisation_once(db):
    """`parent_org_id` cannot express two parents, but a chain can still rejoin: A -> B -> D and
    A -> C, with D later re-pointed under C. Whatever the shape, an organisation is visited once."""
    a = make_org(db, "Diamond Top")
    b = make_org(db, "Diamond Left")
    c = make_org(db, "Diamond Right")
    d = make_org(db, "Diamond Bottom")
    b.parent_org_id = a.id
    c.parent_org_id = a.id
    d.parent_org_id = b.id
    db.commit()
    scope = org_scope(db, a, "all")
    assert sorted(str(i) for i in scope.ids) == sorted(str(i) for i in {a.id, b.id, c.id, d.id})


# ------------------------------------------------------------------ the caps, visibly
def test_the_depth_cap_is_reported_rather_than_silently_applied(db):
    orgs = _chain(db, [f"Deep {i}" for i in range(MAX_DEPTH + 3)])
    db.commit()

    scope = org_scope(db, orgs[0], "all")
    assert scope.depth == MAX_DEPTH
    assert scope.depth_capped is True
    assert scope.organizations == MAX_DEPTH + 1
    assert scope.as_meta()["max_depth"] == MAX_DEPTH
    # The last two organisations in the chain are below the cap and are not in the scope.
    assert orgs[-1].id not in scope.ids


def test_a_chain_exactly_at_the_cap_is_not_flagged_as_capped(db):
    orgs = _chain(db, [f"Exact {i}" for i in range(MAX_DEPTH + 1)])
    db.commit()
    scope = org_scope(db, orgs[0], "all")
    assert scope.depth == MAX_DEPTH and scope.depth_capped is False


def test_the_organisation_cap_truncates_and_says_so(db):
    root = make_org(db, "Wide Fund")
    db.flush()
    for i in range(MAX_SCOPE_ORGS + 5):
        child = make_org(db, f"Portfolio Co {i:04d}")
        child.parent_org_id = root.id
    db.commit()

    scope = org_scope(db, root, "all")
    assert scope.organizations == MAX_SCOPE_ORGS
    assert scope.truncated is True
    assert scope.as_meta()["max_organizations"] == MAX_SCOPE_ORGS


def test_a_merged_away_organisation_is_not_part_of_the_scope(db):
    parent = make_org(db, "Merger Parent")
    live = make_org(db, "Live Sub")
    dead = make_org(db, "Redirected Sub")
    live.parent_org_id = parent.id
    dead.parent_org_id = parent.id
    dead.merged_into_id = live.id
    db.commit()
    assert set(org_scope(db, parent, "all").ids) == {parent.id, live.id}


# ------------------------------------------------------------------ the walk up
def test_ancestors_come_back_nearest_first_and_carry_the_edge_provenance(db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    root, mid, leaf = _chain(db, ["Fund Two", "Midco Two", "Leafco Two"])
    mid.parent_source_id = src.id
    mid.parent_as_of = dt.date(2024, 3, 1)
    leaf.parent_source_id = src.id
    leaf.parent_share_pct = 44.5
    db.commit()

    edges = org_ancestors(db, leaf)
    assert [e.parent.id for e in edges] == [mid.id, root.id]
    assert edges[0].source_id == src.id and edges[0].as_of is None and edges[0].share_pct == 44.5
    assert edges[1].as_of == dt.date(2024, 3, 1) and edges[1].share_pct is None


def test_the_climb_stops_at_the_depth_cap(db):
    orgs = _chain(db, [f"Tall {i}" for i in range(MAX_DEPTH + 4)])
    db.commit()
    assert len(org_ancestors(db, orgs[-1])) == MAX_DEPTH


# ------------------------------------------------------------------ the parameter
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", "self"),
        ("scope=self", "self"),
        ("scope=children", "children"),
        ("scope=all", "all"),
        ("scope=ALL", "all"),
        # The legacy boolean keeps exactly the meaning it was published with.
        ("include_subsidiaries=true", "children"),
        ("include_subsidiaries=1", "children"),
        ("include_subsidiaries=false", "self"),
        ("include_subsidiaries=0", "self"),
    ],
)
def test_scope_from_request_resolves_both_spellings(query, expected):
    assert scope_from_request(_request(query)) == expected


@pytest.mark.parametrize(
    "query",
    ["scope=everything", "include_subsidiaries=yes", "scope=all&include_subsidiaries=true"],
)
def test_scope_from_request_rejects_nonsense_and_the_ambiguous_pair(query):
    with pytest.raises(ProblemError):
        scope_from_request(_request(query))


# ------------------------------------------------------------------ through the endpoints
def _group_with_assets(db):
    """Fund -> Midco -> Operating Co, with one asset on the operating company only: the shape a
    one-level walk gets wrong and the recursive one gets right."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    fund, mid, op = _chain(db, ["Fixture Fund", "Fixture Midco", "Fixture Operating Co"])
    asset = make_asset(db, src, lic, source_asset_id="a1", name="Deep Plant", geom=(-100.0, 32.0))
    make_asset_owner(db, asset, op, src, lic, role="owner", share_pct=100.0)
    db.commit()
    return fund, mid, op


def test_organization_assets_reaches_a_grandchild_only_at_scope_all(client, db):
    fund, _mid, op = _group_with_assets(db)
    path = f"/v1/organizations/{fund.public_id}/assets"

    assert client.get(path).json()["data"] == []
    # One level: the asset sits two levels down, so the legacy parameter still finds nothing --
    # which is the point of not re-pointing it at the recursive walk.
    assert client.get(path, params={"include_subsidiaries": "true"}).json()["data"] == []
    assert client.get(path, params={"scope": "children"}).json()["data"] == []

    body = client.get(path, params={"scope": "all"}).json()
    assert [r["name"] for r in body["data"]] == ["Deep Plant"]
    assert body["scope"]["scope"] == "all" and body["scope"]["organizations"] == 3
    assert body["totals"]["assets"] == 1
    assert [r["organization"]["public_id"] for r in body["totals"]["by_organization"]] == [op.public_id]
    assert body["totals"]["organization_count"] == 1


def test_organization_assets_rejects_the_ambiguous_parameter_pair(client, db):
    fund = _group_with_assets(db)[0]
    resp = client.get(
        f"/v1/organizations/{fund.public_id}/assets",
        params={"scope": "all", "include_subsidiaries": "true"},
    )
    assert resp.status_code == 400


def test_organization_nearby_proposals_scope_reaches_the_whole_group(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    fund, _mid, op = _chain(db, ["Nearby Fund", "Nearby Midco", "Nearby Operating Co"])
    asset = make_asset(db, src, lic, source_asset_id="n1", name="Deep Pipe Plant", geom=(-98.0, 32.0))
    make_asset_owner(db, asset, op, src, lic, role="operator", share_pct=None)
    make_visible_proposal(
        db,
        src,
        public_id_suffix="1",
        location=make_location(db, src, lic, geom=(-98.0, 32.05), precision="exact"),
    )
    db.commit()

    path = f"/v1/organizations/{fund.public_id}/nearby-proposals"
    assert client.get(path).json()["data"] == []
    body = client.get(path, params={"scope": "all"}).json()
    assert len(body["data"]) == 1
    assert body["totals"]["assets_considered"] == 1
    assert body["totals"]["assets_in_scope"] == 1
    assert body["scope"]["depth"] == 2


def test_organization_proposals_scope_spans_the_group_sponsors(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    fund, _mid, op = _chain(db, ["Sponsor Fund", "Sponsor Midco", "Sponsor Operating Co"])
    loc = make_location(db, src, lic, geom=(-98.0, 32.05), precision="exact")
    proposal = make_visible_proposal(db, src, public_id_suffix="1", location=loc, sponsor=op)
    db.commit()

    path = f"/v1/organizations/{fund.public_id}/proposals"
    # A holding company sponsors nothing itself; at `self` the honest answer is an empty page.
    assert client.get(path).json()["data"] == []
    assert client.get(path).json()["scope"]["scope"] == "self"
    body = client.get(path, params={"scope": "all"}).json()
    assert [r["public_id"] for r in body["data"]] == [proposal.public_id]
    assert body["scope"]["organizations"] == 3


# ------------------------------------------------------------------ provenance on the claim
def test_organization_detail_states_the_ownership_chain_with_its_dates_and_nulls(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    root, mid, leaf = _chain(db, ["Provenance Fund", "Provenance Midco", "Provenance Leafco"])
    mid.parent_source_id = src.id
    mid.parent_as_of = dt.date(2024, 6, 30)
    # The curated file states when a page was read, not when the ownership began: no date.
    leaf.parent_source_id = src.id
    db.commit()

    data = client.get(f"/v1/organizations/{leaf.public_id}").json()["data"]
    assert data["parent"]["public_id"] == mid.public_id
    assert data["parent_edge"] == {
        "organization": data["parent"],
        "source_id": src.id,
        "as_of": None,
        "share_pct": None,
    }
    # Root first, so a page renders breadcrumbs straight from it.
    assert [a["organization"]["public_id"] for a in data["ancestors"]] == [
        root.public_id,
        mid.public_id,
    ]
    # Each entry describes the link *below* the organisation it names: the root entry carries the
    # Midco -> Fund claim, so a breadcrumb renders the note one crumb down (`web/ownership.py`).
    assert data["ancestors"][0]["as_of"] == "2024-06-30"
    assert data["ancestors"][1]["as_of"] is None
    assert data["descendant_count"] == 0


def test_organization_detail_group_counts_span_the_whole_descent(client, db):
    fund = _group_with_assets(db)[0]
    data = client.get(f"/v1/organizations/{fund.public_id}").json()["data"]
    assert data["asset_counts"]["assets"] == 0
    assert data["group_asset_counts"]["assets"] == 1
    assert data["subsidiary_count"] == 1 and data["descendant_count"] == 2
    assert data["group_scope"]["depth"] == 2
    assert data["ancestors"] == [] and data["parent_edge"] is None


def test_a_parent_share_percentage_round_trips_when_a_source_states_one(client, db):
    """No loaded source states a stake, so this is the column proving it can hold one rather than
    a measurement of anything in the data (docs/21 §3.5, migration 0017)."""
    _parent, child = _chain(db, ["Stake Parent", "Stake Child"])
    child.parent_share_pct = 30.0
    db.commit()
    data = client.get(f"/v1/organizations/{child.public_id}").json()["data"]
    assert data["parent_edge"]["share_pct"] == 30.0
