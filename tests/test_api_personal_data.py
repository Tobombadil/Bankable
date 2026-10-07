"""Natural persons published as organisations (2026-09-30 legal audit L-5; docs/13 §5.5).

The API side of the conservative default: an organisation the name rule flags carries
`personal_data: true` on every organisation shape, and no ownership share is served on an edge
that names it, on the asset page's owners table or the organisation's own assets list. The row
stays readable: dropping such rows is an owner decision (docs/13 §5.5), not this default.
Names are invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    person = make_org(db, "Dorothy Quillfeather")
    company = make_org(db, "Quillfeather Gas Partners LLC")
    asset = make_asset(db, src, lic, name="Quillfeather Landfill")
    make_asset_owner(db, asset, person, src, lic, share_pct=6.25)
    make_asset_owner(db, asset, company, src, lic, share_pct=93.75)
    proposal = make_visible_proposal(db, src, sponsor=person)
    db.commit()
    return {"person": person, "company": company, "asset": asset, "proposal": proposal}


def test_the_flag_is_served_on_every_organisation_shape(client: TestClient, world: dict[str, Any]) -> None:
    person, company = world["person"], world["company"]
    assert person.personal_data is True and company.personal_data is False
    detail = client.get(f"/v1/organizations/{person.public_id}").json()["data"]
    assert detail["personal_data"] is True
    listed = {o["public_id"]: o["personal_data"] for o in client.get("/v1/organizations").json()["data"]}
    assert listed == {person.public_id: True, company.public_id: False}
    sponsor = client.get(f"/v1/proposals/{world['proposal'].public_id}").json()["data"]["sponsor"]
    assert sponsor["personal_data"] is True


def test_a_persons_ownership_share_is_never_served(client: TestClient, world: dict[str, Any]) -> None:
    owners = client.get(f"/v1/assets/{world['asset'].public_id}").json()["data"]["owners"]
    shares = {o["organization"]["public_id"]: o["share_pct"] for o in owners}
    assert shares == {world["person"].public_id: None, world["company"].public_id: 93.75}
    held = client.get(f"/v1/organizations/{world['person'].public_id}/assets").json()["data"]
    assert [row["share_pct"] for row in held] == [None]
    company_held = client.get(f"/v1/organizations/{world['company'].public_id}/assets").json()["data"]
    assert [row["share_pct"] for row in company_held] == [93.75]
