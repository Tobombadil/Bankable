"""An owner edge's raw register spelling must not name a taken-down organisation (2026-09-27, lane E16).

Lane E14 withheld an asset's `operator_name` and attribute strings that name a non-public organisation,
and the takedown lane drops `owners[]` edges *to* one. Left open by lane E15: an edge to a **public**
organisation carries `owner_name_raw`, the register's own spelling, and that spelling can share an
`org_key` with a non-public organisation. Before this change `GET /v1/assets/{id}` printed it.

The rule (services/api/withheld_names.py, "Owner edges' raw spellings"): on every non-admin surface an
`owner_name_raw` whose `org_key` is a withheld key is `null`; the edge stays and shows the public
organisation it points to. Conservative, as E14's operator rule: a key shared by a hidden and a public
organisation is withheld.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import services.api.assets as assets_module
from services.api import withheld_names
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_open_licence,
    make_org,
    make_public_source,
)
from services.db.models import Organization
from web.api_client import build_client
from web.app import app as web_app

HIDDEN = "Shadow Midstream Partners"
#: How the register spelt the owner on an edge the resolver pointed at the public organisation.
RAW_HIDDEN_SPELLING = "SHADOW MIDSTREAM PARTNERS, LLC"
#: A public organisation and a hidden one that share a key (E14's "matches both" case).
TWIN_PUBLIC = "Twin Energy"
TWIN_HIDDEN = "TWIN ENERGY LLC"
TWIN_RAW = "Twin Energy, Inc."
SPEC_PATH = pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"
BBOX = {"bbox": "-110,20,-80,45", "zoom": "6"}


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    assets_module._reset_asset_index_cache()
    withheld_names.reset_cache()
    yield
    assets_module._reset_asset_index_cache()
    withheld_names.reset_cache()


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    hidden = make_org(db, HIDDEN)
    public_owner = make_org(db, "Open Owner Holdings")
    twin_public = make_org(db, TWIN_PUBLIC)
    twin_hidden = make_org(db, TWIN_HIDDEN)
    plant = make_asset(db, src, lic, source_asset_id="p1", name="Prairie Gas Plant", geom=(-98.0, 32.0))
    plant.operator_name = "Open Owner Holdings"
    spelled = make_asset_owner(db, plant, public_owner, src, lic, role="owner", share_pct=60.0)
    spelled.owner_name_raw = RAW_HIDDEN_SPELLING
    control = make_asset_owner(db, plant, twin_public, src, lic, role="owner", share_pct=40.0)
    control.owner_name_raw = "Open Owner Holdings"  # a public spelling: never touched
    twin_asset = make_asset(db, src, lic, source_asset_id="p2", name="Twin Solar", geom=(-97.0, 31.0))
    twin_edge = make_asset_owner(db, twin_asset, twin_public, src, lic, role="owner", share_pct=100.0)
    twin_edge.owner_name_raw = TWIN_RAW
    db.commit()
    return {
        "hidden": hidden,
        "twin_hidden": twin_hidden,
        "public_owner": public_owner,
        "twin_public": twin_public,
        "plant": plant,
        "twin_asset": twin_asset,
    }


def _set_state(db: Session, org: Organization, state: str) -> None:
    org.publish_state = state
    db.commit()


def _owners(client: TestClient, asset: Any) -> list[dict[str, Any]]:
    resp = client.get(f"/v1/assets/{asset.public_id}")
    assert resp.status_code == 200, resp.text
    owners: list[dict[str, Any]] = resp.json()["data"]["owners"]
    return sorted(owners, key=lambda o: -(o["share_pct"] or 0))


def _surfaces(client: TestClient, world: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset detail": client.get(f"/v1/assets/{world['plant'].public_id}").json(),
        "twin asset detail": client.get(f"/v1/assets/{world['twin_asset'].public_id}").json(),
        "asset list": client.get("/v1/assets", params={"limit": 50}).json(),
        "public owner's asset list": client.get(
            f"/v1/organizations/{world['public_owner'].public_id}/assets"
        ).json(),
        "twin owner's asset list": client.get(
            f"/v1/organizations/{world['twin_public'].public_id}/assets"
        ).json(),
        "asset map": client.get("/v1/assets/geo", params=BBOX).json(),
    }


def test_before_takedown_the_raw_spelling_is_printed(client: TestClient, world: dict[str, Any]) -> None:
    """The fixture is live: with every organisation public, the raw spelling is served as stored."""
    assert world["hidden"].publish_state == world["twin_hidden"].publish_state == "public"
    spelled, control = _owners(client, world["plant"])
    assert spelled["owner_name_raw"] == RAW_HIDDEN_SPELLING
    assert control["owner_name_raw"] == "Open Owner Holdings"
    assert _owners(client, world["twin_asset"])[0]["owner_name_raw"] == TWIN_RAW


@pytest.mark.parametrize("state", ["unpublished", "pending_review"])
def test_an_edge_to_a_public_owner_does_not_print_a_raw_spelling_of_a_taken_down_one(
    client: TestClient, db: Session, world: dict[str, Any], state: str
) -> None:
    _set_state(db, world["hidden"], state)
    spelled, control = _owners(client, world["plant"])
    # The edge stays, pointing at (and naming) the public organisation; only the raw string goes.
    assert spelled["organization"]["public_id"] == world["public_owner"].public_id
    assert spelled["organization"]["name_canonical"] == "Open Owner Holdings"
    assert spelled["share_pct"] == 60.0 and spelled["role"] == "owner"
    assert spelled["owner_name_raw"] is None
    assert control["owner_name_raw"] == "Open Owner Holdings"
    for where, payload in _surfaces(client, world).items():
        assert HIDDEN.lower() not in json.dumps(payload).lower(), f"{where} names the taken-down organisation"


def test_a_key_shared_by_a_public_and_a_hidden_organisation_is_withheld(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    """E14's conservative side: the edge's own organisation is public and stays named by its canonical
    name, but a raw spelling whose key also names a hidden organisation is not printed."""
    _set_state(db, world["twin_hidden"], "unpublished")
    [edge] = _owners(client, world["twin_asset"])
    assert edge["organization"]["name_canonical"] == TWIN_PUBLIC
    assert edge["owner_name_raw"] is None
    assert TWIN_RAW.lower() not in json.dumps(_surfaces(client, world)).lower()


def test_the_null_raw_spelling_is_in_the_documented_schema(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _set_state(db, world["hidden"], "unpublished")
    spec = yaml.safe_load(SPEC_PATH.read_text())
    schema = spec["components"]["schemas"]["AssetOwnerRow"]
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    for edge in _owners(client, world["plant"]):
        jsonschema.validators.validator_for(schema)(schema, resolver=resolver).validate(edge)


def test_republishing_restores_the_raw_spelling(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _set_state(db, world["hidden"], "unpublished")
    assert _owners(client, world["plant"])[0]["owner_name_raw"] is None
    _set_state(db, world["hidden"], "public")
    assert _owners(client, world["plant"])[0]["owner_name_raw"] == RAW_HIDDEN_SPELLING


@pytest.fixture()
def web_client(client: TestClient) -> Iterator[TestClient]:
    """The site against the real API in process, on the database the `client` fixture overrides."""
    with TestClient(web_app) as wc:
        web_app.state.api_client = build_client(api_base_url="")
        web_app.state.lag_days_default = None
        yield wc
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def test_the_web_asset_and_organisation_pages_do_not_print_the_raw_spelling(
    web_client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _set_state(db, world["hidden"], "unpublished")
    for path in (
        f"/assets/{world['plant'].slug}",
        f"/organizations/{world['public_owner'].slug}",
    ):
        resp = web_client.get(path)
        assert resp.status_code == 200, (path, resp.status_code)
        assert HIDDEN.lower() not in resp.text.lower(), f"{path} names the taken-down organisation"
        assert "Open Owner Holdings" in resp.text  # the public owner the edge points to is still shown
