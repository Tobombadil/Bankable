"""An asset's register text must not name a taken-down organisation (docs/00-PLAN.md 2026-09-27,
"Organisations can be taken down": `asset.operator_name` was left open as "register text that can
still name a taken-down organisation").

The takedown lane dropped the organisation's *edges* on every non-admin surface, but the asset row
keeps the source's own operator string (`asset.operator_name`) and a few attribute keys that carry
company strings (`operator_raw`, `owner_raw`, `atlas_operator_name`, `attributes.phmsa.operator_name`
and `operator_id`). Before this change every one of them still printed the withheld name: on the
asset page and list, the organisation-assets list of a *public* co-owner, both map layers (points
and lines), the web asset page and search, and `GET /v1/assets?q=` found the asset by that name.

The rule (services/api/withheld_names.py): an asset's operator name is withheld when the asset has an
`operator` edge to an organisation that is not public, or when its `operator_name` has the same
`org_key` as a non-public organisation's name or alias; any attribute string with such a key is
dropped too. Assets carry no tier (ADR 0008), so every surface applies it; there is no admin asset
read. Republishing the organisation restores every name.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import services.api.assets as assets_module
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_open_licence,
    make_org,
    make_public_source,
)
from services.db.models import Organization, OrganizationAlias
from web.api_client import build_client
from web.app import app as web_app

HIDDEN = "Shadow Midstream Partners"
#: How the register spells it: a different case and a legal form, same `org_key`.
HIDDEN_REGISTER_SPELLING = "SHADOW MIDSTREAM PARTNERS, LLC"
#: A spelling only an alias ties to the organisation (PHMSA's style of abbreviation).
HIDDEN_ALIAS = "Shadow Mdstm Ptnrs"
BBOX = {"bbox": "-110,20,-80,45", "zoom": "6"}


@pytest.fixture(autouse=True)
def _fresh_indexes() -> Iterator[None]:
    assets_module._reset_asset_index_cache()
    yield
    assets_module._reset_asset_index_cache()


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    hidden = make_org(db, HIDDEN)
    visible_owner = make_org(db, "Open Owner Holdings")
    db.add(_alias(hidden, src))
    # 1. Operated by the organisation through an edge, register spelling in `operator_name`, and
    #    co-owned by a public organisation (so it is on that organisation's public asset list).
    plant = make_asset(db, src, lic, source_asset_id="p1", name="Prairie Gas Plant", geom=(-98.0, 32.0))
    plant.operator_name = HIDDEN_REGISTER_SPELLING
    plant.attributes = {
        "operator_raw": HIDDEN_REGISTER_SPELLING,
        "owner_raw": "Open Owner Holdings",
        "city": "Midland",
        "phmsa": {"operator_name": HIDDEN_ALIAS.upper(), "operator_id": "31234", "report_year": 2024},
    }
    make_asset_owner(db, plant, hidden, src, lic, role="operator", share_pct=None)
    make_asset_owner(db, plant, visible_owner, src, lic, role="owner", share_pct=100.0)
    # 2. No edge at all: only the register string names the organisation (by `org_key`).
    compressor = make_asset(
        db, src, lic, source_asset_id="p2", name="Mesa Compressor", asset_type="gas_processing_plant"
    )
    compressor.operator_name = HIDDEN.upper()
    # 3. A pipeline line: the line layer carries `operator_name` and `attributes.operator`.
    line = make_asset(
        db, src, lic, source_asset_id="l1", name="Shadow Line", asset_type="gas_pipeline", technology=None
    )
    line.geom = None
    line.geom_line = [(-99.0, 31.0), (-97.0, 33.0)]
    line.operator_name = HIDDEN
    line.attributes = {"operator": HIDDEN, "miles": 120.0, "pipeline_type": "Interstate"}
    # 4. Control: a public operator's name is never touched.
    control = make_asset(db, src, lic, source_asset_id="c1", name="Control Plant", geom=(-97.5, 32.5))
    control.operator_name = "Open Owner Holdings"
    make_asset_owner(db, control, visible_owner, src, lic, role="operator", share_pct=None)
    db.commit()
    return {
        "hidden": hidden,
        "visible_owner": visible_owner,
        "plant": plant,
        "compressor": compressor,
        "line": line,
        "control": control,
    }


def _alias(org: Organization, src: Any) -> OrganizationAlias:
    return OrganizationAlias(
        organization_id=org.id,
        alias=HIDDEN_ALIAS,
        alias_normalised=HIDDEN_ALIAS.lower(),
        kind="filing_spelling",
        source_id=src.id,
        source_url=src.url,
        retrieved_at=_now(),
        licence_id=src.licence_id,
        confidence=1.0,
        created_by="pipeline",
    )


def _now() -> Any:
    import datetime as dt

    return dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def _take_down(db: Session, org: Organization, state: str = "unpublished") -> None:
    org.publish_state = state
    db.commit()


def _names_in(payload: Any) -> str:
    import json

    return json.dumps(payload).lower()


def _assert_no_hidden_name(payload: Any, where: str) -> None:
    text = _names_in(payload)
    for spelling in (HIDDEN, HIDDEN_REGISTER_SPELLING, HIDDEN_ALIAS):
        assert spelling.lower() not in text, f"{where} names the taken-down organisation ({spelling!r})"
    assert "31234" not in text, f"{where} carries the taken-down operator's PHMSA id"


def _surfaces(client: TestClient, world: dict[str, Any]) -> dict[str, Any]:
    plant, compressor, line = world["plant"], world["compressor"], world["line"]
    return {
        "asset detail (edge)": client.get(f"/v1/assets/{plant.public_id}").json(),
        "asset detail (name only)": client.get(f"/v1/assets/{compressor.public_id}").json(),
        "asset detail (line)": client.get(f"/v1/assets/{line.public_id}").json(),
        "asset list": client.get("/v1/assets", params={"limit": 50}).json(),
        "public co-owner's asset list": client.get(
            f"/v1/organizations/{world['visible_owner'].public_id}/assets"
        ).json(),
        "asset map (points and lines)": client.get("/v1/assets/geo", params=BBOX).json(),
    }


def test_before_takedown_every_surface_names_the_operator(client: TestClient, world: dict[str, Any]) -> None:
    """The fixture is live: with the organisation public, the name is where the test looks."""
    surfaces = _surfaces(client, world)
    assert surfaces["asset detail (edge)"]["data"]["operator_name"] == HIDDEN_REGISTER_SPELLING
    assert surfaces["asset detail (name only)"]["data"]["operator_name"] == HIDDEN.upper()
    geo = surfaces["asset map (points and lines)"]
    line_feature = next(
        f for f in geo["data"]["features"] if f["properties"].get("feature_kind") == "asset_line"
    )
    assert line_feature["properties"]["operator_name"] == HIDDEN
    assert line_feature["properties"]["attributes"]["operator"] == HIDDEN
    found = client.get("/v1/assets", params={"q": "shadow midstream"}).json()["data"]
    assert {a["public_id"] for a in found} >= {world["plant"].public_id, world["compressor"].public_id}


@pytest.mark.parametrize("state", ["unpublished", "pending_review"])
def test_no_api_surface_names_a_taken_down_operator(
    client: TestClient, db: Session, world: dict[str, Any], state: str
) -> None:
    _take_down(db, world["hidden"], state)
    surfaces = _surfaces(client, world)
    for where, payload in surfaces.items():
        _assert_no_hidden_name(payload, where)
    plant = surfaces["asset detail (edge)"]["data"]
    assert plant["operator_name"] is None
    # Non-name attributes survive; only the withheld strings go.
    assert plant["attributes"]["city"] == "Midland"
    assert plant["attributes"]["owner_raw"] == "Open Owner Holdings"
    assert plant["attributes"]["phmsa"]["report_year"] == 2024
    assert surfaces["asset detail (name only)"]["data"]["operator_name"] is None
    line = surfaces["asset detail (line)"]["data"]
    assert line["operator_name"] is None and line["attributes"]["miles"] == 120.0
    # The assets themselves stay: the register fact is public; only the name is withheld.
    listed = {a["public_id"] for a in surfaces["asset list"]["data"]}
    assert {world["plant"].public_id, world["compressor"].public_id, world["line"].public_id} <= listed
    control = client.get(f"/v1/assets/{world['control'].public_id}").json()["data"]
    assert control["operator_name"] == "Open Owner Holdings"


def test_search_does_not_find_an_asset_by_a_withheld_operator_name(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _take_down(db, world["hidden"])
    for q in ("shadow midstream", "SHADOW MIDSTREAM PARTNERS, LLC"):
        found = client.get("/v1/assets", params={"q": q}).json()["data"]
        # "Shadow Line" is found by its own name, which is not the organisation's; nothing else is.
        assert {a["public_id"] for a in found} <= {world["line"].public_id}, q
        _assert_no_hidden_name(found, f"search {q!r}")
    # The asset is still found by its own name, with the operator withheld.
    by_name = client.get("/v1/assets", params={"q": "prairie gas"}).json()["data"]
    assert [a["operator_name"] for a in by_name] == [None]


def test_republishing_restores_the_name_on_every_surface_including_the_cached_line_layer(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    client.get("/v1/assets/geo", params=BBOX)  # warm the line index while the organisation is public
    _take_down(db, world["hidden"])
    _assert_no_hidden_name(client.get("/v1/assets/geo", params=BBOX).json(), "warm line index after takedown")
    _take_down(db, world["hidden"], "public")
    geo = client.get("/v1/assets/geo", params=BBOX).json()
    line_feature = next(
        f for f in geo["data"]["features"] if f["properties"].get("feature_kind") == "asset_line"
    )
    assert line_feature["properties"]["operator_name"] == HIDDEN
    assert client.get(f"/v1/assets/{world['plant'].public_id}").json()["data"]["operator_name"] == (
        HIDDEN_REGISTER_SPELLING
    )


@pytest.fixture()
def web_client(client: TestClient) -> Iterator[TestClient]:
    """The site against the real API in process (`build_client(api_base_url="")`), on the same
    database the `client` fixture overrides."""
    with TestClient(web_app) as wc:
        web_app.state.api_client = build_client(api_base_url="")
        web_app.state.lag_days_default = None
        yield wc
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def test_the_web_asset_page_and_search_do_not_name_a_taken_down_operator(
    web_client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    before = web_client.get(f"/assets/{world['plant'].slug}")
    assert before.status_code == 200 and HIDDEN_REGISTER_SPELLING.lower() in before.text.lower()
    _take_down(db, world["hidden"])
    for path in (
        f"/assets/{world['plant'].slug}",
        f"/assets/{world['compressor'].slug}",
        f"/assets/{world['line'].slug}",
        f"/organizations/{world['visible_owner'].slug}",
        "/search?q=shadow+midstream",
    ):
        resp = web_client.get(path)
        assert resp.status_code == 200, (path, resp.status_code)
        text = resp.text.lower()
        # The search box echoes the reader's own "shadow midstream"; the full name must not appear.
        for spelling in (HIDDEN, HIDDEN_REGISTER_SPELLING, HIDDEN_ALIAS):
            assert spelling.lower() not in text, f"{path} names the taken-down organisation"
    results = web_client.get("/search?q=shadow+midstream").text
    # Found only by the withheld operator name before the fix; not found at all now.
    assert world["plant"].slug not in results and world["compressor"].slug not in results
