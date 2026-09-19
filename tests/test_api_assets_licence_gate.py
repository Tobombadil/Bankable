"""Asset licence gate (2026-09-18 audit, docs/50 §3.1 "the asset endpoint had no licence gate";
docs/21 §8 checklist items 1 and 4): an asset under a `restricted`/`unknown` licence, or from a
source not on the public surface, is absent from the list, `404` on detail and nearby, absent
from the geo index (including after a licence flip, through the cache key), from the
organisation's assets and from the plants alias. Everything goes through
`services/api/visibility.py::asset_visibility_filter` (`visible_asset_predicate`).
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.api import assets as assets_module
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_open_licence,
    make_org,
    make_public_source,
)
from services.api.visibility import asset_visibility_filter, visible_asset_predicate
from services.db.models import Licence, Source

UTC = dt.UTC
WORLD_BBOX = "-180,-85,180,85"


@pytest.fixture(autouse=True)
def _reset_asset_index_cache() -> None:
    assets_module._reset_asset_index_cache()


def _gated_licence(db, reuse_class: str, id_: str) -> Licence:
    lic = Licence(id=id_, name=f"{reuse_class} licence", reuse_class=reuse_class, gate_flag=True)
    db.add(lic)
    db.flush()
    return lic


def _source(db, licence: Licence, id_: str, publish_state: str = "public") -> Source:
    src = Source(
        id=id_,
        name=f"Source {id_}",
        category="registry",
        jurisdiction="US",
        operator="Op",
        url=f"https://example.org/{id_}",
        access="bulk_file",
        cadence="monthly",
        licence_id=licence.id,
        publish_state=publish_state,
        manifest_version="2026-09-18",
        manifest_hash="0" * 64,
    )
    db.add(src)
    db.flush()
    return src


def _seed(db, reuse_class: str):
    """One visible asset (open licence, public source) and one gated asset with the given class."""
    open_lic = make_open_licence(db)
    open_src = make_public_source(db, open_lic)
    visible = make_asset(db, open_src, open_lic, source_asset_id="1", name="Visible Plant")
    gated_lic = _gated_licence(db, reuse_class, f"{reuse_class}-lic")
    gated_src = _source(db, gated_lic, f"us.test.{reuse_class}_source")
    gated = make_asset(db, gated_src, gated_lic, source_asset_id="9", name="Gated Plant", geom=(-100.5, 32.5))
    org = make_org(db, "Owner Co")
    make_asset_owner(db, visible, org, open_src, open_lic)
    make_asset_owner(db, gated, org, gated_src, gated_lic)
    db.commit()
    return visible, gated, org


@pytest.mark.parametrize("reuse_class", ["restricted", "unknown"])
def test_gated_asset_absent_from_every_read_path(client, db, reuse_class):
    visible, gated, org = _seed(db, reuse_class)

    body = client.get("/v1/assets").json()
    assert [a["public_id"] for a in body["data"]] == [visible.public_id]
    assert all(row["reuse_class"] in ("open", "attribution") for row in body["licence_summary"]["sources"])

    assert client.get(f"/v1/assets/{gated.public_id}").status_code == 404
    assert client.get(f"/v1/assets/{visible.public_id}").status_code == 200

    geo = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 4}).json()
    ids = {f["properties"]["public_id"] for f in geo["data"]["features"]}
    assert ids == {visible.public_id}
    assert geo["data"]["totals"]["records"] == 1
    assert all(row["reuse_class"] in ("open", "attribution") for row in geo["licence_summary"]["sources"])

    plants = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4}).json()
    assert {f["properties"]["public_id"] for f in plants["data"]["features"]} == {visible.public_id}

    assert client.get(f"/v1/assets/{gated.public_id}/nearby-proposals").status_code == 404
    assert client.get(f"/v1/assets/{visible.public_id}/nearby-proposals").status_code == 200

    org_assets = client.get(f"/v1/organizations/{org.public_id}/assets").json()
    assert [a["public_id"] for a in org_assets["data"]] == [visible.public_id]

    # The gated name and slug never appear anywhere in any of those bodies.
    for resp in (body, geo, plants, org_assets):
        assert "Gated Plant" not in str(resp)
        assert gated.slug not in str(resp)


def test_gated_owner_edge_omitted_from_visible_asset_detail(client, db):
    visible, _gated, _org = _seed(db, "restricted")
    restricted_lic = db.get(Licence, "restricted-lic")
    restricted_src = db.get(Source, "us.test.restricted_source")
    other = make_org(db, "Shadow Holdings")
    make_asset_owner(db, visible, other, restricted_src, restricted_lic, role="operator", share_pct=None)
    db.commit()

    data = client.get(f"/v1/assets/{visible.public_id}").json()["data"]
    assert [o["organization"]["name_canonical"] for o in data["owners"]] == ["Owner Co"]
    assert all(o["provenance"]["reuse_class"] in ("open", "attribution") for o in data["owners"])


def test_source_not_on_public_surface_hides_its_assets(client, db):
    open_lic = make_open_licence(db)
    public_src = make_public_source(db, open_lic)
    api_only_src = _source(db, open_lic, "us.test.api_only_source", publish_state="api_only")
    shown = make_asset(db, public_src, open_lic, source_asset_id="1", name="Shown")
    make_asset(db, api_only_src, open_lic, source_asset_id="2", name="Hidden", geom=(-101.0, 33.0))
    db.commit()

    assert [a["public_id"] for a in client.get("/v1/assets").json()["data"]] == [shown.public_id]
    geo = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 4}).json()
    assert {f["properties"]["public_id"] for f in geo["data"]["features"]} == {shown.public_id}


def test_licence_flip_invalidates_the_geo_index_cache(client, db):
    open_lic = make_open_licence(db)
    src = make_public_source(db, open_lic)
    asset = make_asset(db, src, open_lic, source_asset_id="1", name="Flipped Plant")
    db.commit()

    before = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 4}).json()
    assert {f["properties"]["public_id"] for f in before["data"]["features"]} == {asset.public_id}

    # Reclassify the licence with no change to any asset row: `asset.last_changed` is untouched,
    # so only a cache key that watches `licence.updated_at` notices.
    open_lic.reuse_class = "restricted"
    open_lic.updated_at = dt.datetime.now(UTC) + dt.timedelta(seconds=1)
    db.commit()

    after = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 4}).json()
    assert after["data"]["features"] == []
    assert after["data"]["totals"]["records"] == 0
    assert client.get(f"/v1/assets/{asset.public_id}").status_code == 404


def test_predicate_names_are_the_same_object():
    assert visible_asset_predicate is asset_visibility_filter
