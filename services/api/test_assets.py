"""Tests for `services/api/assets.py`: `GET /v1/assets`, `/{public_id}`, `/geo`,
`/{public_id}/nearby-proposals`, `GET /v1/organizations/{public_id}/assets`, and the
`GET /v1/context/plants/geo` alias (ADR 0008)."""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import json
import pathlib

import pytest
import yaml

from services.api import assets as assets_module
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import Licence
from tests.test_api_contract import assert_valid

UTC = dt.UTC
_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
WORLD_BBOX = "-180,-85,180,85"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(autouse=True)
def _reset_asset_index_cache() -> None:
    assets_module._reset_asset_index_cache()


# ------------------------------------------------------------------------------------ list/detail
def test_list_assets_basic(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="1", name="Roscoe Wind Farm")
    make_asset(db, src, lic, source_asset_id="2", name="Bay Peaker", technology="gas_ct")
    db.commit()

    resp = client.get("/v1/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetListResponse", body)
    assert len(body["data"]) == 2
    names = {a["name"] for a in body["data"]}
    assert names == {"Roscoe Wind Farm", "Bay Peaker"}


def test_list_assets_filters_by_asset_type_and_technology(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="1", technology="wind")
    make_asset(db, src, lic, source_asset_id="2", technology="solar")
    db.commit()

    resp = client.get("/v1/assets", params={"technology": "solar"})
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["technology"] == "solar"


def test_list_assets_filters_by_organization(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    a1 = make_asset(db, src, lic, source_asset_id="1", name="Owned Plant")
    make_asset(db, src, lic, source_asset_id="2", name="Unowned Plant")
    org = make_org(db, "Owner Co")
    make_asset_owner(db, a1, org, src, lic)
    db.commit()

    resp = client.get("/v1/assets", params={"organization": org.public_id})
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "Owned Plant"


def test_list_assets_q_matches_owner_name(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    a1 = make_asset(db, src, lic, source_asset_id="1", name="Plant A")
    org = make_org(db, "NextEra Energy Resources LLC")
    make_asset_owner(db, a1, org, src, lic)
    db.commit()

    resp = client.get("/v1/assets", params={"q": "nextera"})
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "Plant A"


def test_get_asset_detail_includes_owners_and_attributes(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    asset = make_asset(db, src, lic, source_asset_id="1", name="Roscoe Wind Farm")
    asset.attributes = {"capacity_factor_2025": 0.41}
    org = make_org(db, "NextEra Energy Resources LLC")
    make_asset_owner(db, asset, org, src, lic, role="owner", share_pct=75.0)
    db.commit()

    resp = client.get(f"/v1/assets/{asset.public_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetDetailResponse", body)
    data = body["data"]
    assert data["public_id"] == asset.public_id
    assert data["attributes"] == {"capacity_factor_2025": 0.41}
    assert len(data["owners"]) == 1
    assert data["owners"][0]["organization"]["public_id"] == org.public_id
    assert data["owners"][0]["share_pct"] == 75.0


def test_get_asset_detail_lists_every_source_once_resolved(client, db, spec):
    """docs/24 §5(a): once `asset_source` links exist, a detail response lists every one, primary
    first, not just the primary's own quartet (`services/api/serialize.py::asset_provenance_rows`)."""
    from services.api.conftest import make_asset_source

    lic = make_open_licence(db)
    atlas = make_public_source(db, lic, id_="us.eia.atlas.ethanol_plants")
    report = make_public_source(db, lic, id_="us.eia.ethanol_capacity")
    asset = make_asset(
        db, atlas, lic, source_asset_id="NE-poet-fairmont", asset_type="ethanol_plant", name="Poet Fairmont"
    )
    make_asset_source(db, asset, atlas, lic, is_primary=True, match_method="deterministic_key")
    make_asset_source(
        db,
        asset,
        report,
        lic,
        source_record_id="NE-flint-hills-fairmont",
        is_primary=False,
        match_method="rule",
        match_score=0.767,
    )
    db.commit()

    resp = client.get(f"/v1/assets/{asset.public_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetDetailResponse", body)
    provenance = body["data"]["provenance"]
    assert len(provenance) == 2
    assert provenance[0]["source_id"] == "us.eia.atlas.ethanol_plants"
    assert provenance[0]["is_primary"] is True
    assert provenance[0]["match_method"] == "deterministic_key"
    assert provenance[0]["match_score"] is None
    assert provenance[1]["source_id"] == "us.eia.ethanol_capacity"
    assert provenance[1]["is_primary"] is False
    assert provenance[1]["match_method"] == "rule"
    assert provenance[1]["match_score"] == pytest.approx(0.767)


def test_get_asset_detail_falls_back_to_its_own_quartet_with_no_links_yet(client, db, spec):
    """Every asset type but `ethanol_plant` has no `asset_source` rows yet -- the detail response
    must still show exactly the one source the asset itself carries, unchanged from before this
    table existed."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    asset = make_asset(db, src, lic, source_asset_id="1", name="Roscoe Wind Farm")
    db.commit()

    resp = client.get(f"/v1/assets/{asset.public_id}")
    body = resp.json()
    assert_valid(spec, "AssetDetailResponse", body)
    provenance = body["data"]["provenance"]
    assert len(provenance) == 1
    assert provenance[0]["source_id"] == src.id
    assert provenance[0]["is_primary"] is True
    assert provenance[0]["match_method"] == "deterministic_key"
    assert provenance[0]["match_score"] is None


def test_get_asset_404_for_unknown_public_id(client, db):
    resp = client.get("/v1/assets/asset_doesnotexist")
    assert resp.status_code == 404


# ------------------------------------------------------------------------------------------ geo
def test_assets_geo_individual_features(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="1", geom=(-100.0, 32.0))
    make_asset(db, src, lic, source_asset_id="2", geom=(-99.0, 31.0))
    db.commit()

    resp = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetGeoResponse", body)
    fc = body["data"]
    assert len(fc["features"]) == 2
    assert fc["totals"]["clustered"] is False
    kinds = {f["properties"]["feature_kind"] for f in fc["features"]}
    assert kinds == {"asset"}


def test_assets_geo_clusters_above_split_threshold(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i in range(600):
        make_asset(db, src, lic, source_asset_id=str(i), geom=(-100.0 + (i % 5) * 0.01, 32.0))
    db.commit()

    resp = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetGeoResponse", body)
    fc = body["data"]
    assert fc["totals"]["clustered"] is True
    assert fc["totals"]["records"] == 600
    assert all(f["properties"]["feature_kind"] == "asset_cluster" for f in fc["features"])
    assert all("dominant_asset_type" in f["properties"] for f in fc["features"])


def test_assets_geo_asset_type_filter(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="1", asset_type="power_plant", geom=(-100.0, 32.0))
    db.commit()

    resp = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "asset_type": "gas_pipeline"})
    assert resp.status_code == 200
    assert resp.json()["data"]["totals"]["records"] == 0


def test_assets_geo_unknown_asset_type_is_400(client, db):
    resp = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "asset_type": "not_real"})
    assert resp.status_code == 400


# --------------------------------------------------------------------- context/plants/geo alias
def test_context_plants_geo_alias_forces_power_plant(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="1", asset_type="power_plant", geom=(-100.0, 32.0))
    db.commit()

    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["totals"]["records"] == 1
    assert body["features"][0]["properties"]["asset_type"] == "power_plant"


def test_context_plants_geo_alias_rejects_asset_type_param(client, db):
    resp = client.get(
        "/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "asset_type": "power_plant"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_parameter"


def test_context_plants_geo_alias_unknown_query_parameter_is_400(client, db):
    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "bogus": "x"})
    assert resp.status_code == 400


# ------------------------------------------------------------------------------- nearby-proposals
def test_nearby_proposals_returns_exact_precision_only_within_radius(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    asset = make_asset(db, src, lic, source_asset_id="1", geom=(-100.0, 32.0))

    near_loc = make_location(db, src, lic, geom=(-100.01, 32.01), precision="exact")
    far_loc = make_location(db, src, lic, geom=(10.0, 10.0), precision="exact")
    region_loc = make_location(db, src, lic, geom=(-100.0, 32.0), precision="county_centroid")
    make_visible_proposal(db, src, public_id_suffix="1", location=near_loc)
    make_visible_proposal(db, src, public_id_suffix="2", location=far_loc)
    make_visible_proposal(db, src, public_id_suffix="3", location=region_loc)
    db.commit()

    resp = client.get(f"/v1/assets/{asset.public_id}/nearby-proposals")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "NearbyProposalsResponse", body)
    assert len(body["data"]) == 1
    assert body["data"][0]["public_id"].startswith("prop_")
    assert "distance_km" in body["data"][0]


def test_nearby_proposals_radius_km_validated(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    asset = make_asset(db, src, lic, source_asset_id="1", geom=(-100.0, 32.0))
    db.commit()

    resp = client.get(f"/v1/assets/{asset.public_id}/nearby-proposals", params={"radius_km": 200})
    assert resp.status_code == 400


def test_nearby_proposals_404_for_unknown_asset(client, db):
    resp = client.get("/v1/assets/asset_doesnotexist/nearby-proposals")
    assert resp.status_code == 404


# ------------------------------------------------------------------------ organization -> assets
def test_organization_assets_lists_role_and_share(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Owner Co")
    asset = make_asset(db, src, lic, source_asset_id="1", name="Owned Plant")
    make_asset_owner(db, asset, org, src, lic, role="owner", share_pct=60.0)
    db.commit()

    resp = client.get(f"/v1/organizations/{org.public_id}/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["role"] == "owner"
    assert body["data"][0]["share_pct"] == 60.0


def test_organization_assets_404_for_unknown_org(client, db):
    resp = client.get("/v1/organizations/org_doesnotexist/assets")
    assert resp.status_code == 404


def _named_source(db, lic, id_, name, publish_state="public"):
    src = make_public_source(db, lic, id_=id_)
    src.name, src.url, src.publish_state = name, f"https://example.org/{id_}", publish_state
    db.flush()
    return src


def _restricted_licence(db):
    lic = Licence(
        id="restricted-lic",
        name="Restricted Licence",
        reuse_class="restricted",
        allows_derived_publication=False,
        allows_raw_publication=False,
        allows_api_redistribution=False,
        allows_bulk_export=False,
        allows_commercial_use=False,
        gate_flag=True,
        evidence_url="https://example.org/restricted",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def test_organization_assets_filter_on_status_technology_and_state(client, db):
    """2026-10-21: the filters the portfolio's counts name (`services/api/org_portfolio.py`). The
    stored token is matched, as on `GET /v1/assets`: `depleted_field` is no proposal technology."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Filter Co")
    rows = [
        ("Gas Plant", "power_plant", "gas_cc", "US-TX", "operating"),
        ("Old Coal", "power_plant", "coal", "US-OH", "retired"),
        ("Wind Farm", "power_plant", "wind", "US-TX", "retiring"),
        ("Storage Field", "gas_storage", "depleted_field", "US-OK", "standby"),
    ]
    for i, (name, type_, tech, state, status) in enumerate(rows):
        asset = make_asset(
            db,
            src,
            lic,
            source_asset_id=str(i),
            name=name,
            asset_type=type_,
            technology=tech,
            state_code=state,
        )
        asset.status = status
        make_asset_owner(db, asset, org, src, lic)
    db.commit()
    path = f"/v1/organizations/{org.public_id}/assets"

    def names(**params):
        resp = client.get(path, params=params)
        assert resp.status_code == 200, resp.text
        return sorted(r["name"] for r in resp.json()["data"])

    assert names(status="operating,standby,retiring") == ["Gas Plant", "Storage Field", "Wind Farm"]
    assert names(status="retired") == ["Old Coal"]
    assert names(technology="depleted_field") == ["Storage Field"]
    assert names(technology="gas_cc,wind", state="US-TX") == ["Gas Plant", "Wind Farm"]
    assert names(state="US-TX", status="retiring") == ["Wind Farm"]
    assert names(technology="unobtanium") == []  # a value nothing stores selects nothing
    assert client.get(path, params={"status": "planned"}).status_code == 400
    # `totals` still counts the whole set, whatever the filters.
    assert client.get(path, params={"status": "retired"}).json()["totals"]["assets"] == 4


def test_organization_assets_sort_by_capacity_pages_largest_first_one_asset_at_a_time(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Sorted Co")
    sub = make_org(db, "Sorted Sub")
    sub.parent_org_id = org.id
    for i, mw in enumerate([100.0, 300.0, None, 300.0, 50.5, None]):
        asset = make_asset(db, src, lic, source_asset_id=str(i), name=f"Plant {i}", capacity_mw=mw)
        make_asset_owner(db, asset, org, src, lic)
        if i % 2 == 0:  # a second edge on some: the two rows of one asset must stay adjacent
            make_asset_owner(db, asset, sub, src, lic, share_pct=None)
    db.commit()
    path = f"/v1/organizations/{org.public_id}/assets"

    seen, cursor, pages = [], None, 0
    while True:
        params = {
            "sort": "-capacity_mw",
            "scope": "children",
            "limit": 2,
            **({"cursor": cursor} if cursor else {}),
        }
        body = client.get(path, params=params).json()
        seen.extend((r["name"], r["capacity_mw"], r["held_by"]["name_canonical"]) for r in body["data"])
        cursor, pages = body["page"]["next_cursor"], pages + 1
        if not cursor:
            break
    assert pages == 5 and len(seen) == 9 and len(set(seen)) == 9  # every edge once
    capacities = [mw for _, mw, _ in seen]
    stated = [mw for mw in capacities if mw is not None]
    assert stated == sorted(stated, reverse=True) and capacities[len(stated) :] == [None] * 3  # MW-less last
    # One run per asset: its two rows are adjacent even across a page boundary.
    runs = [name for name, _ in itertools.groupby(name for name, _, _ in seen)]
    assert len(runs) == len(set(runs)) == 6

    assert client.get(path, params={"sort": "capacity_mw"}).status_code == 400
    assert client.get(path, params={"sort": "name"}).json()["errors"][0]["field"] == "sort"
    # A cursor is valid only for the order it came from.
    default_cursor = client.get(path, params={"limit": 1}).json()["page"]["next_cursor"]
    capacity_cursor = client.get(path, params={"limit": 1, "sort": "-capacity_mw"}).json()["page"][
        "next_cursor"
    ]
    assert client.get(path, params={"sort": "-capacity_mw", "cursor": default_cursor}).status_code == 400
    assert client.get(path, params={"cursor": capacity_cursor}).status_code == 400
    nil = "00000000-0000-0000-0000-000000000000"
    for payload in (
        {"v": "abc", "id": f"{nil}/{nil}"},
        {"v": "1.5", "id": f"{nil}/x"},
        {"v": None, "id": "y/"},
        {"v": "1.5", "id": nil},  # a default-order id: no asset half
    ):
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        resp = client.get(path, params={"sort": "-capacity_mw", "cursor": raw})
        assert resp.status_code == 400 and resp.json()["code"] == "invalid_cursor", payload


def test_organization_assets_rows_carry_the_edge_provenance_and_credit_its_register(client, db, spec):
    lic = make_open_licence(db)
    plants = _named_source(db, lic, "us.test.plants", "Test Plant Register")
    owners = _named_source(db, lic, "us.test.owner_shares", "Test Owner Shares")
    org = make_org(db, "Provenance Co")
    asset = make_asset(db, plants, lic, source_asset_id="1", name="Credited Plant")
    edge = make_asset_owner(db, asset, org, owners, lic, share_pct=25.0)
    edge.source_url = "https://example.org/us.test.owner_shares#row-7"
    edge.retrieved_at = dt.datetime(2026, 10, 9, 22, 27, tzinfo=UTC)
    db.commit()

    body = client.get(f"/v1/organizations/{org.public_id}/assets").json()
    assert_valid(spec, "OrganizationAssetsResponse", body)
    row = body["data"][0]
    assert row["edge_provenance"] == {
        "source_id": "us.test.owner_shares",
        "source_name": "Test Owner Shares",
        "source_url": "https://example.org/us.test.owner_shares#row-7",
        "retrieved_at": "2026-10-09T22:27:00Z",
        "licence_id": "open-lic",
        "reuse_class": "open",
        "attribution_text": None,
    }
    assert [p["source_id"] for p in row["provenance"]] == ["us.test.plants"]  # the asset's own
    credited = {s["source_id"]: s for s in body["licence_summary"]["sources"]}
    assert set(credited) == {"us.test.plants", "us.test.owner_shares"}
    assert credited["us.test.owner_shares"]["retrieved_at_max"] == "2026-10-09T22:27:00Z"
    assert all(s["record_count"] == 1 for s in credited.values())


def test_organization_assets_omit_an_edge_only_a_hidden_register_states(client, db):
    """docs/21 §8 item 3: the asset detail already drops such an edge from `owners[]`; until
    2026-10-21 the organisation's own list still served it as a row and counted it in `totals`."""
    lic = make_open_licence(db)
    plants = _named_source(db, lic, "us.test.plants", "Test Plant Register")
    restricted = _named_source(db, _restricted_licence(db), "us.test.restricted_owners", "Restricted Owners")
    staged = _named_source(db, lic, "us.test.staged_owners", "Staged Owners", publish_state="ingest_only")
    org = make_org(db, "Shadow Holder")
    shown = make_asset(db, plants, lic, source_asset_id="1", name="Openly Held Plant")
    make_asset_owner(db, shown, org, plants, lic)
    gated = make_asset(db, plants, lic, source_asset_id="2", name="Quietly Held Plant")
    make_asset_owner(db, gated, org, restricted, db.get(Licence, "restricted-lic"))
    make_asset_owner(db, gated, org, staged, lic, role="operator", share_pct=None)
    db.commit()

    resp = client.get(f"/v1/organizations/{org.public_id}/assets")
    body = resp.json()
    assert [r["name"] for r in body["data"]] == ["Openly Held Plant"]
    assert body["totals"]["assets"] == 1 and body["totals"]["by_role"] == {"owner": 1}
    for text in ("Quietly Held Plant", "us.test.restricted_owners", "us.test.staged_owners"):
        assert text not in resp.text
    # The organisation page's counts read the same totals.
    assert client.get(f"/v1/organizations/{org.public_id}").json()["data"]["asset_counts"]["assets"] == 1
    # The asset stays visible on its own: only the edge is a hidden register's statement.
    assert client.get(f"/v1/assets/{gated.public_id}").json()["data"]["owners"] == []


def test_list_assets_filters_by_slug(client, db):
    """The asset page resolves its slug through the list endpoint, as proposal pages do
    (live probe 2026-09-18: `/v1/assets?slug=…` was rejected as an unknown parameter)."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    wanted = make_asset(db, src, lic, source_asset_id="1", name="Roscoe Wind Farm")
    make_asset(db, src, lic, source_asset_id="2", name="Bay Peaker", technology="gas_ct")
    db.commit()

    resp = client.get("/v1/assets", params={"slug": wanted.slug})
    assert resp.status_code == 200, resp.text
    assert [a["public_id"] for a in resp.json()["data"]] == [wanted.public_id]
    assert client.get("/v1/assets", params={"slug": "no-such-asset"}).json()["data"] == []
