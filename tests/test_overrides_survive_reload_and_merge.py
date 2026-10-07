"""An admin edit is a human decision and sticks (docs/21 §6.4: "the normaliser and the enricher skip
overridden fields until a user clears the override"; docs/13 §5.4 rule 6: "a deletion that a later
crawl silently undoes is not a deletion").

L-1 of the 2026-09-30 legal audit: `PATCH /admin/v1/proposals/{id}` stored the edit in
`overrides`, and the next load of the record's source wrote the register's spelling straight back,
because nothing in the loader or the resolver read `overrides`. Covered here: the admin route then a
reload; a reload with the override cleared; a merge that absorbs an overridden record (the human
decision moves to the survivor, and a later load of the absorbed record's source does not undo it);
the survivor's own override winning; and an unmerge putting everything back.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Proposal, ProposalSource, Source
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.resolve.merge import merge_proposal, unmerge_proposal
from tests.conftest import login, make_account, make_user

REDACTED = "NY - Larkspur Ridge project 2"
REGISTER_SPELLING = "NY - 12 Larkspur Ridge Rd - 2"


def _entry(source_id: str) -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": source_id,
            "name": f"Test {source_id}",
            "jurisdiction": "US-NY",
            "category": "generation_queue",
            "operator": "Test ISO",
            "url": f"https://example.org/{source_id}",
            "access": "bulk_file",
            "reuse": "open",
            "publication": "raw_ok",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def _row(source_id: str, record_id: str, name: str, mw: float) -> dict[str, Any]:
    return {
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "source_record_id": record_id,
        "source_url": f"https://example.org/{source_id}/{record_id}",
        "retrieved_at": "2026-09-13T20:25:31Z",
        "kind": "generation",
        "name_canonical": name,
        "sponsor_name": None,
        "technology": "solar",
        "technology_raw": "Solar",
        "capacity_mw": mw,
        "storage_mwh": None,
        "iso": "NYISO",
        "state": "NY",
        "county": None,
        "lifecycle_state": "filed",
        "status_raw": "Active",
        "proposed_cod": None,
        "queue_id": record_id,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "raw": f'{{"Queue Pos.": "{record_id}"}}',
    }


def _load(db: Session, source: Source, rows: list[dict[str, Any]]) -> None:
    load_dataframe(db, source, "proposal", pd.DataFrame(rows), None)
    db.commit()


def _proposal(db: Session, source_id: str, record_id: str) -> Proposal:
    link = db.scalar(
        select(ProposalSource).where(
            ProposalSource.source_id == source_id, ProposalSource.source_record_id == record_id
        )
    )
    assert link is not None
    prop = db.get(Proposal, link.proposal_id)
    assert prop is not None
    return prop


def _admin(client: TestClient, db: Session) -> None:
    account = make_account(db, entitlement="admin", name="Ops")
    user = make_user(db, account, email="operator@example.com", role="operator")
    db.commit()
    login(client, db, user)


def _override(prop: Proposal, field: str, value: Any) -> None:
    setattr(prop, field, value)
    prop.overrides = {
        **(prop.overrides or {}),
        field: {"value": value, "event_id": "evt_test", "user_id": "usr_test"},
    }


def test_an_admin_edit_survives_the_next_load_of_its_source(client: TestClient, db: Session) -> None:
    source = upsert_licence_and_source(db, _entry("us.test.nyiso"), "2026-09-18")
    _load(db, source, [_row("us.test.nyiso", "1234", REGISTER_SPELLING, 5.0)])
    prop = _proposal(db, "us.test.nyiso", "1234")
    _admin(client, db)

    resp = client.patch(
        f"/admin/v1/proposals/{prop.public_id}",
        json={"name_canonical": REDACTED, "reason": "privacy: street address of a private individual"},
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()

    # The register reloads with its own spelling and a new capacity.
    _load(db, source, [_row("us.test.nyiso", "1234", REGISTER_SPELLING, 7.5)])
    db.expire_all()
    prop = _proposal(db, "us.test.nyiso", "1234")
    assert prop.name_canonical == REDACTED
    assert "name_canonical" in prop.overrides
    assert float(prop.capacity_mw or 0) == 7.5  # an un-overridden field still follows the source
    # The source's own view lands on its link row, so nothing is lost.
    link = prop.sources[0]
    assert link.normalised["name_canonical"] == REGISTER_SPELLING
    assert client.get(f"/v1/proposals/{prop.public_id}").json()["data"]["name_canonical"] == REDACTED

    # Clearing the override hands the field back to the source at its next load.
    prop.overrides = {}
    db.commit()
    _load(db, source, [_row("us.test.nyiso", "1234", REGISTER_SPELLING, 7.5)])
    db.expire_all()
    assert _proposal(db, "us.test.nyiso", "1234").name_canonical == REGISTER_SPELLING


def test_a_merge_carries_the_absorbed_records_override_and_a_reload_does_not_undo_it(db: Session) -> None:
    queue = upsert_licence_and_source(db, _entry("us.test.queue"), "2026-09-18")
    inventory = upsert_licence_and_source(db, _entry("us.test.inventory"), "2026-09-18")
    _load(db, queue, [_row("us.test.queue", "Q1", REGISTER_SPELLING, 5.0)])
    _load(db, inventory, [_row("us.test.inventory", "P1", "Larkspur Ridge Solar", 5.0)])
    absorbed = _proposal(db, "us.test.queue", "Q1")
    survivor = _proposal(db, "us.test.inventory", "P1")
    _override(absorbed, "name_canonical", REDACTED)
    db.commit()

    event = merge_proposal(db, canonical=survivor, absorbed=absorbed, score=92.0, rationale="test")
    db.commit()
    assert survivor.name_canonical == REDACTED
    assert "name_canonical" in survivor.overrides
    assert event.after["surviving"]["overrides_carried"] == ["name_canonical"]

    # Both registers load again; the queue's link now points at the survivor.
    _load(db, queue, [_row("us.test.queue", "Q1", REGISTER_SPELLING, 6.0)])
    _load(db, inventory, [_row("us.test.inventory", "P1", "Larkspur Ridge Solar", 6.0)])
    db.expire_all()
    survivor = db.get(Proposal, survivor.id)
    assert survivor is not None and survivor.name_canonical == REDACTED

    # Unmerge puts the survivor's own value and overrides back; the absorbed row keeps its own.
    unmerge_proposal(db, event.id)
    db.commit()
    db.expire_all()
    survivor = db.get(Proposal, survivor.id)
    absorbed = db.get(Proposal, absorbed.id)
    assert survivor is not None and absorbed is not None
    assert survivor.name_canonical == "Larkspur Ridge Solar"
    assert "name_canonical" not in (survivor.overrides or {})
    assert absorbed.name_canonical == REDACTED and "name_canonical" in absorbed.overrides


def test_the_survivors_own_override_wins_over_the_absorbed_records(db: Session) -> None:
    queue = upsert_licence_and_source(db, _entry("us.test.queue"), "2026-09-18")
    inventory = upsert_licence_and_source(db, _entry("us.test.inventory"), "2026-09-18")
    _load(db, queue, [_row("us.test.queue", "Q1", REGISTER_SPELLING, 5.0)])
    _load(db, inventory, [_row("us.test.inventory", "P1", "Larkspur Ridge Solar", 5.0)])
    absorbed = _proposal(db, "us.test.queue", "Q1")
    survivor = _proposal(db, "us.test.inventory", "P1")
    _override(absorbed, "name_canonical", REDACTED)
    _override(survivor, "name_canonical", "Larkspur Ridge (operator's name)")
    _override(survivor, "identifiers", {"queue_ids": []})
    db.commit()

    event = merge_proposal(db, canonical=survivor, absorbed=absorbed, score=92.0, rationale="test")
    db.commit()
    assert survivor.name_canonical == "Larkspur Ridge (operator's name)"
    assert survivor.identifiers == {"queue_ids": []}  # a pinned `identifiers` takes no carried basis
    assert "overrides_carried" not in (event.after or {}).get("surviving", {})
