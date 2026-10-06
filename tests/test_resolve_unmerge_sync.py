"""An unmerge is a change that `updated_since` and bulk sync see (backend audit 2026-09-30 F6).

Before, unmerge restored both rows' `last_changed` (and `updated_at`) to their values before the
merge, which moved them backwards: a consumer syncing with `updated_since=<the time of its last
sync>` never saw the survivor lose a source, nor the restored record reappear.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_org, make_public_source, make_visible_proposal
from services.db.models import Organization, Proposal
from services.resolve.merge import merge_organization, merge_proposal, unmerge_organization, unmerge_proposal
from tests.conftest import make_account, make_api_key, make_user

UTC = dt.UTC


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _merged_pair(db: Session) -> tuple[Proposal, Proposal, Any]:
    source = make_public_source(db, make_open_licence(db))
    survivor = make_visible_proposal(db, source, public_id_suffix="1")
    absorbed = make_visible_proposal(db, source, public_id_suffix="2")
    event = merge_proposal(db, canonical=survivor, absorbed=absorbed, score=95.0, rationale="test")
    db.commit()
    return survivor, absorbed, event


def test_unmerge_moves_both_proposals_forward_for_updated_since(client: TestClient, db: Session) -> None:
    survivor, absorbed, merge_event = _merged_pair(db)
    synced_at = dt.datetime.now(UTC) + dt.timedelta(milliseconds=5)
    before = client.get("/v1/proposals", params={"updated_since": _iso(synced_at), "limit": 200})
    assert before.status_code == 200 and before.json()["data"] == []

    unmerge_proposal(db, merge_event.id)
    db.commit()

    after = client.get("/v1/proposals", params={"updated_since": _iso(synced_at), "limit": 200})
    ids = {row["public_id"] for row in after.json()["data"]}
    assert ids == {survivor.public_id, absorbed.public_id}


def test_unmerge_reaches_a_bulk_sync(client: TestClient, db: Session) -> None:
    survivor, absorbed, merge_event = _merged_pair(db)
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()
    synced_at = dt.datetime.now(UTC) + dt.timedelta(milliseconds=5)

    unmerge_proposal(db, merge_event.id)
    db.commit()

    resp = client.get(
        "/v1/bulk/proposals",
        params={"updated_since": _iso(synced_at)},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 200, resp.text
    records = [json.loads(line) for line in resp.iter_lines() if line][1:]
    assert {r["public_id"] for r in records} == {survivor.public_id, absorbed.public_id}


def test_unmerge_moves_both_organisations_forward(db: Session) -> None:
    canonical, absorbed = make_org(db, "Acme Power LLC"), make_org(db, "Acme Power Holdings")
    event = merge_organization(db, canonical=canonical, absorbed=absorbed, rationale="test")
    db.commit()
    synced_at = dt.datetime.now(UTC)

    unmerge_organization(db, event.id)
    db.commit()

    for row in db.scalars(select(Organization)).all():
        assert row.last_changed.replace(tzinfo=UTC) >= synced_at, row.name_canonical
        assert row.updated_at.replace(tzinfo=UTC) >= synced_at, row.name_canonical
