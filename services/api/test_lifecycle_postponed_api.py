"""The API serves `postponed` (owner decision 2026-10-24): it is in the vocabularies, it filters the proposal
list, a stored postponed record is served with it, and the published definitions page carries its definition
and the EIA-860M mapping into it."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal


def _values(vocab: Any) -> list[str]:
    return [v["value"] if isinstance(v, dict) else v for v in vocab]


def test_the_vocabularies_name_postponed(client: Any) -> None:
    body = client.get("/v1/meta/vocabularies").json()
    assert "postponed" in _values(body["data"]["lifecycle_state"])


def test_the_list_filters_on_postponed_and_serves_it(client: Any, db: Session) -> None:
    source = make_public_source(db, make_open_licence(db))
    make_visible_proposal(db, source, public_id_suffix="1", lifecycle_state="postponed")
    make_visible_proposal(db, source, public_id_suffix="2", lifecycle_state="filed")
    db.commit()
    rows = client.get("/v1/proposals", params={"lifecycle_state": "postponed"}).json()["data"]
    assert [r["lifecycle_state"] for r in rows] == ["postponed"]
    active = client.get("/v1/proposals", params={"lifecycle_state": "announced,filed"}).json()["data"]
    assert [r["lifecycle_state"] for r in active] == ["filed"]
    assert client.get("/v1/events", params={"to_state": "postponed"}).status_code == 200


def test_the_definitions_page_defines_postponed_and_shows_the_eia_mapping(client: Any) -> None:
    body = client.get("/v1/lifecycle-states").json()
    states = {s["state"]: s for s in body["data"]["lifecycle_states"]}
    postponed = states["postponed"]
    assert "canceled or indefinitely postponed" in postponed["definition"]
    raws = {(m["source_id"], m["raw"]) for m in postponed["maps_from"]}
    assert ("us.eia.860m", "Canceled or Postponed") in raws
