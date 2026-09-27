"""A record that is public through one source must not list its links to a gated one
(docs/21 §8 item 3, 2026-09-26 M-11 audit finding).

The record predicate (`services/api/visibility.py`) asks only that *some* active link be
permitted, so a proposal seen in both an open register and PJM is public on the strength of the
open evidence -- correctly -- but every surface that then lists its links one by one used to print
the PJM link too: `GET /v1/{proposals,opportunities}/{id}/sources`, the `provenance` array on list
and detail, the `licence_summary` built from the same links, the feed item's credited source, and
an asset's `asset_source` and `asset_owner` rows. §8 item 3: "the Sources panel omits the row
entirely rather than showing a greyed placeholder, because the existence of the row is itself a
disclosure".

Two kinds of gated link are exercised, one per clause: a `restricted` licence on a source whose
`publish_state` is `public` (`licence_permits` fails), and an open licence on a source still at
`ingest_only` (`source_permits` fails). A third, `api_only`, is visible to a Pro key and not to an
anonymous reader, which is what proves the tier reaches the link filter. Admin reads still list
every link.
"""

from __future__ import annotations

import datetime as dt
import types
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import services.api.visibility as visibility
from services.alerts.feed import matching_items_for_feed
from services.alerts.matching import matches_query
from services.api.admin_records import router as admin_records_router
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_asset_source,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.api.errors import ProblemError, problem_exception_handler
from services.db.models import Licence, OpportunitySource, ProposalSource, Source
from tests.conftest import login, make_account, make_api_key, make_user

UTC = dt.UTC
NOW = dt.datetime.now(UTC)
GATED_NAMES = ("PJM Restricted Queue", "Staged Ingest Register")


def _restricted_licence(db: Session) -> Licence:
    lic = Licence(
        id="pjm-restricted",
        name="PJM Manual 14 terms",
        reuse_class="restricted",
        allows_derived_publication=False,
        allows_raw_publication=False,
        allows_api_redistribution=False,
        allows_bulk_export=False,
        allows_commercial_use=False,
        gate_flag=True,
        evidence_url="https://example.org/pjm-terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def _source(db: Session, lic: Licence, id_: str, name: str, publish_state: str) -> Source:
    src = make_public_source(db, lic, id_=id_)
    src.name = name
    src.url = f"https://example.org/{id_}"
    src.publish_state = publish_state
    db.flush()
    return src


def _link(db: Session, model: type[Any], fk: str, record_id: Any, src: Source, record_key: str) -> None:
    db.add(
        model(
            **{fk: record_id},
            source_id=src.id,
            source_record_id=record_key,
            source_url=f"{src.url}#{record_key}",
            retrieved_at=NOW - dt.timedelta(days=1),
            licence_id=src.licence_id,
            raw={"id": record_key},
            first_seen=NOW - dt.timedelta(days=3),
            last_seen=NOW - dt.timedelta(days=1),
        )
    )
    db.flush()


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    open_lic = make_open_licence(db)
    restricted = _restricted_licence(db)
    open_src = make_public_source(db, open_lic, id_="us.test.open_register")
    pjm = _source(db, restricted, "us.iso.pjm.test_queue", "PJM Restricted Queue", "public")
    staged = _source(db, open_lic, "us.test.staged_register", "Staged Ingest Register", "ingest_only")
    api_only = _source(db, open_lic, "us.test.api_only_register", "Api Only Register", "api_only")

    # Placed exactly, so the proposals map draws it as a record feature carrying `provenance`.
    loc = make_location(db, open_src, open_lic, geom=(-98.0, 32.05), precision="exact")
    proposal = make_visible_proposal(db, open_src, public_id_suffix="1", location=loc)
    opportunity = make_visible_opportunity(db, open_src, public_id_suffix="1")
    for src in (pjm, staged, api_only):
        _link(db, ProposalSource, "proposal_id", proposal.id, src, f"P-{src.id}")
        _link(db, OpportunitySource, "opportunity_id", opportunity.id, src, f"O-{src.id}")

    asset = make_asset(db, open_src, open_lic, source_asset_id="a1", name="Linked Plant")
    make_asset_source(db, asset, open_src, open_lic, is_primary=True)
    make_asset_source(db, asset, pjm, restricted, source_record_id="pjm-1", is_primary=False)
    make_asset_source(db, asset, staged, open_lic, source_record_id="staged-1", is_primary=False)
    make_asset_owner(db, asset, make_org(db, "Openly Stated Owner"), open_src, open_lic)
    make_asset_owner(db, asset, make_org(db, "Pjm Stated Owner"), pjm, restricted, role="operator")
    make_asset_owner(db, asset, make_org(db, "Staged Stated Owner"), staged, open_lic, role="operator")
    db.commit()
    return {
        "open": open_src,
        "pjm": pjm,
        "staged": staged,
        "api_only": api_only,
        "proposal": proposal,
        "opportunity": opportunity,
        "asset": asset,
    }


def _source_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {r["source_id"] for r in rows}


def _assert_no_gated_text(text: str) -> None:
    for name in (*GATED_NAMES, "us.iso.pjm.test_queue", "us.test.staged_register", "pjm-restricted"):
        assert name not in text, name


# --------------------------------------------------------------------------- the helpers
def test_the_link_helpers_apply_both_clauses_per_tier(db: Session, world: dict[str, Any]) -> None:
    links = list(world["proposal"].sources)
    assert {link.source_id for link in visibility.visible_source_links(links)} == {world["open"].id}
    assert {link.source_id for link in visibility.visible_source_links(links, "pro")} == {
        world["open"].id,
        world["api_only"].id,
    }
    links[0].active = False
    assert world["open"].id not in {link.source_id for link in visibility.visible_source_links(links[:1])}
    assert visibility.permitted_source_states("admin") == ("public",)  # unknown tier: public's set
    assert not visibility.provenance_visible(world["open"], world["pjm"].licence)
    assert visibility.provenance_visible(world["open"], world["open"].licence)


# ------------------------------------------------------------------------- proposals
def test_proposal_sources_panel_detail_list_and_licence_summary_omit_gated_links(
    client: TestClient, world: dict[str, Any]
) -> None:
    prop = world["proposal"]
    open_id = world["open"].id

    panel = client.get(f"/v1/proposals/{prop.public_id}/sources")
    assert panel.status_code == 200
    assert _source_ids(panel.json()["data"]) == {open_id}
    assert _source_ids(panel.json()["licence_summary"]["sources"]) == {open_id}
    _assert_no_gated_text(panel.text)

    detail = client.get(f"/v1/proposals/{prop.public_id}")
    assert _source_ids(detail.json()["data"]["provenance"]) == {open_id}
    assert _source_ids(detail.json()["licence_summary"]["sources"]) == {open_id}
    _assert_no_gated_text(detail.text)

    listed = client.get("/v1/proposals")
    [row] = listed.json()["data"]
    assert _source_ids(row["provenance"]) == {open_id}
    assert _source_ids(listed.json()["licence_summary"]["sources"]) == {open_id}
    _assert_no_gated_text(listed.text)

    geo = client.get("/v1/proposals/geo", params={"bbox": "-180,-90,180,90", "zoom": "4"})
    assert geo.status_code == 200
    assert _source_ids(geo.json()["licence_summary"]["sources"]) <= {open_id}

    for path in ("/feeds/proposals.rss", "/feeds/proposals.json"):
        _assert_no_gated_text(client.get(path).text)


def test_a_pro_key_sees_the_api_only_link_and_nothing_gated(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    account = make_account(db, entitlement="pro")
    _key, secret = make_api_key(db, account, make_user(db, account))
    db.commit()
    headers = {"Authorization": f"Bearer {secret}"}
    expected = {world["open"].id, world["api_only"].id}
    prop, opp = world["proposal"], world["opportunity"]

    assert (
        _source_ids(client.get(f"/v1/proposals/{prop.public_id}/sources", headers=headers).json()["data"])
        == expected
    )
    assert (
        _source_ids(
            client.get(f"/v1/proposals/{prop.public_id}", headers=headers).json()["data"]["provenance"]
        )
        == expected
    )
    assert (
        _source_ids(client.get(f"/v1/opportunities/{opp.public_id}/sources", headers=headers).json()["data"])
        == expected
    )
    anonymous = client.get(f"/v1/proposals/{prop.public_id}/sources").json()["data"]
    assert _source_ids(anonymous) == {world["open"].id}


# ----------------------------------------------------------------------- opportunities
def test_opportunity_sources_panel_detail_and_list_omit_gated_links(
    client: TestClient, world: dict[str, Any]
) -> None:
    opp = world["opportunity"]
    open_id = world["open"].id

    panel = client.get(f"/v1/opportunities/{opp.public_id}/sources")
    assert panel.status_code == 200
    assert _source_ids(panel.json()["data"]) == {open_id}
    _assert_no_gated_text(panel.text)

    detail = client.get(f"/v1/opportunities/{opp.public_id}")
    assert _source_ids(detail.json()["data"]["provenance"]) == {open_id}
    assert _source_ids(detail.json()["licence_summary"]["sources"]) == {open_id}
    _assert_no_gated_text(detail.text)

    listed = client.get("/v1/opportunities")
    [row] = listed.json()["data"]
    assert _source_ids(row["provenance"]) == {open_id}
    _assert_no_gated_text(listed.text)
    for path in ("/feeds/opportunities.rss", "/feeds/opportunities.json"):
        _assert_no_gated_text(client.get(path).text)


# -------------------------------------------------------------------------------- assets
def test_asset_source_links_and_owner_edges_omit_gated_provenance(
    client: TestClient, world: dict[str, Any]
) -> None:
    asset = world["asset"]
    detail = client.get(f"/v1/assets/{asset.public_id}")
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert _source_ids(data["provenance"]) == {world["open"].id}
    assert [o["organization"]["name_canonical"] for o in data["owners"]] == ["Openly Stated Owner"]
    _assert_no_gated_text(detail.text)
    assert "Pjm Stated Owner" not in detail.text and "Staged Stated Owner" not in detail.text


def test_asset_provenance_falls_back_to_its_own_quartet_when_every_link_is_gated(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    """Never an empty provenance array: the asset passed its own predicate on `asset.source_id`."""
    asset = world["asset"]
    for link in asset.sources:
        if link.source_id == world["open"].id:
            db.delete(link)
    db.commit()
    rows = client.get(f"/v1/assets/{asset.public_id}").json()["data"]["provenance"]
    assert _source_ids(rows) == {world["open"].id}
    assert rows[0]["is_primary"] is True


# --------------------------------------------------------------------------------- admin
@pytest.fixture()
def admin_client(db_sessionmaker: sessionmaker[Session]) -> TestClient:
    def _override():
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    app = FastAPI()
    app.add_exception_handler(ProblemError, problem_exception_handler)
    app.include_router(admin_records_router)
    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c


def test_admin_reads_still_list_every_link(
    admin_client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    account = make_account(db, entitlement="admin", name="Ops")
    login(admin_client, db, make_user(db, account, email="ops@example.com", role="operator"))
    everything = {world[k].id for k in ("open", "pjm", "staged", "api_only")}
    for path in (
        f"/admin/v1/proposals/{world['proposal'].public_id}",
        f"/admin/v1/opportunities/{world['opportunity'].public_id}",
    ):
        data = admin_client.get(path).json()["data"]
        assert _source_ids(data["provenance"]) == everything, path
        assert _source_ids(data["sources"]) == everything, path
        # Admin has no filtered record list; what an operator matches a gated link by is its record
        # id, and the ungated read still carries it.
        record_ids = {r["source_record_id"] for r in data["sources"]}
        assert any(rid and rid.endswith(world["pjm"].id) for rid in record_ids), path


# --------------------------------------------------------------------- map and saved-search feeds
def _pro_headers(db: Session) -> dict[str, str]:
    account = make_account(db, entitlement="pro", name="Pro Reader")
    _key, secret = make_api_key(db, account, make_user(db, account, email="pro-reader@example.com"))
    db.commit()
    return {"Authorization": f"Bearer {secret}"}


def _geo_feature_provenance(
    client: TestClient, headers: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    resp = client.get("/v1/proposals/geo", params={"bbox": "-180,-90,180,90", "zoom": "4"}, headers=headers)
    assert resp.status_code == 200
    [feature] = [f for f in resp.json()["data"]["features"] if f["properties"]["feature_kind"] == "proposal"]
    return feature["properties"]["provenance"]


def test_proposal_map_feature_provenance_omits_gated_links_per_tier(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    open_id, api_only_id = world["open"].id, world["api_only"].id
    assert _source_ids(_geo_feature_provenance(client)) == {open_id}
    _assert_no_gated_text(
        client.get("/v1/proposals/geo", params={"bbox": "-180,-90,180,90", "zoom": "4"}).text
    )
    pro = _geo_feature_provenance(client, _pro_headers(db))
    assert _source_ids(pro) == {open_id, api_only_id}


def _feed_items(db: Session, entity: str, entitlement: str) -> list[dict[str, Any]]:
    """`matching_items_for_feed` reads only `search.entity`/`search.query` and
    `account.entitlement`, so the tier contrast is driven directly; the HTTP route is the next test."""
    search = types.SimpleNamespace(entity=entity, query={})
    account = types.SimpleNamespace(entitlement=entitlement)
    return matching_items_for_feed(db, search, account)  # type: ignore[arg-type]


@pytest.mark.parametrize("entity", ["proposal", "opportunity"])
def test_saved_search_feed_items_credit_only_links_the_owner_tier_may_read(
    db: Session, world: dict[str, Any], entity: str
) -> None:
    open_id, api_only_id = world["open"].id, world["api_only"].id
    for entitlement, expected in (("public", {open_id}), ("pro", {open_id, api_only_id})):
        [item] = _feed_items(db, entity, entitlement)
        summary = item["platform_ext"]["licence_summary"]["sources"]
        assert _source_ids(summary) == expected, entitlement
        assert item["creator"] not in GATED_NAMES
        _assert_no_gated_text(repr(item))


def test_the_saved_search_rss_route_for_a_pro_owner_carries_no_gated_link(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    account = make_account(db, entitlement="pro", name="Feed Owner")
    login(client, db, make_user(db, account, email="feed-owner@example.com"))
    created = client.post(
        "/v1/saved-searches",
        json={"name": "everything", "entity": "proposal", "query": {}, "channels": ["email", "rss"]},
    ).json()["data"]
    token = created["rss_url"].rsplit("/", 1)[-1]
    body = client.get(f"/feeds/saved/{token}?format=json")
    assert body.status_code == 200
    [item] = body.json()["items"]
    assert _source_ids(item["_platform"]["licence_summary"]["sources"]) == {
        world["open"].id,
        world["api_only"].id,
    }
    _assert_no_gated_text(body.text)
    _assert_no_gated_text(client.get(f"/feeds/saved/{token}").text)


# ------------------------------------------------------------------ filters that match through a link
def _ids(
    client: TestClient, path: str, params: dict[str, str], headers: dict[str, str] | None = None
) -> list[str]:
    resp = client.get(path, params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return [r["public_id"] for r in resp.json()["data"]]


@pytest.mark.parametrize(
    ("path", "record", "prefix"),
    [("/v1/proposals", "proposal", "P"), ("/v1/opportunities", "opportunity", "O")],
)
def test_source_id_and_q_do_not_match_through_a_gated_link(
    client: TestClient, db: Session, world: dict[str, Any], path: str, record: str, prefix: str
) -> None:
    """The record is visible through the open source; asking for it by a gated source's id, or by
    the gated link's own record id, must answer exactly as if that link did not exist."""
    target = world[record].public_id
    assert _ids(client, path, {"source_id": world["open"].id}) == [target]
    for gated in ("pjm", "staged", "api_only"):
        src_id = world[gated].id
        assert _ids(client, path, {"source_id": src_id}) == [], gated
        assert _ids(client, path, {"q": f"{prefix}-{src_id}"}) == [], gated

    headers = _pro_headers(db)
    api_only = world["api_only"].id
    assert _ids(client, path, {"source_id": api_only}, headers) == [target]
    assert _ids(client, path, {"q": f"{prefix}-{api_only}"}, headers) == [target]
    for gated in ("pjm", "staged"):
        src_id = world[gated].id
        assert _ids(client, path, {"source_id": src_id}, headers) == [], gated
        assert _ids(client, path, {"q": f"{prefix}-{src_id}"}, headers) == [], gated


def test_the_map_does_not_draw_or_count_a_record_through_a_gated_source_id(
    client: TestClient, world: dict[str, Any]
) -> None:
    params = {"bbox": "-180,-90,180,90", "zoom": "4", "source_id": world["pjm"].id}
    body = client.get("/v1/proposals/geo", params=params).json()
    assert body["data"]["features"] == []
    assert body["data"]["totals"]["records"] == 0
    assert body["licence_summary"]["sources"] == []


@pytest.mark.parametrize("entity", ["proposal", "opportunity"])
def test_a_saved_search_on_a_gated_source_id_matches_nothing_for_its_owner_tier(
    db: Session, world: dict[str, Any], entity: str
) -> None:
    def matched(source_key: str, entitlement: str) -> int:
        search = types.SimpleNamespace(entity=entity, query={"source_id": world[source_key].id})
        account = types.SimpleNamespace(entitlement=entitlement)
        return len(matching_items_for_feed(db, search, account))  # type: ignore[arg-type]

    assert matched("open", "public") == 1
    for gated in ("pjm", "staged", "api_only"):
        assert matched(gated, "public") == 0, gated
    assert matched("api_only", "pro") == 1
    assert matched("pjm", "pro") == 0 and matched("staged", "pro") == 0
    # The row-level matcher the alert digest and webhooks call fails closed without a tier.
    row = world[entity]
    assert not matches_query(entity, row, {"source_id": world["api_only"].id})
    assert matches_query(entity, row, {"source_id": world["api_only"].id}, "pro")
