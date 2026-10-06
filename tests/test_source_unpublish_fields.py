"""Unpublishing a source withholds its *field values*, not only its link row (docs/21 §8, the
mixed-provenance case; QA-1 of the 2026-09-30 platform audit).

The audit's case, rebuilt small: a proposal two registers report -- a queue (`us.test.queue`, the
ERCOT role) and an inventory (`us.test.inventory`, the EIA-860M role) -- whose stored fields all
came from the inventory (`field_provenance`), with the inventory's exact point as its placement.
An operator then sets the inventory to `ingest_only`. Before 2026-10-06 the record kept printing
the inventory's name, capacity, status text and plant ids under a Sources panel that listed only
the queue, `source_count` stayed 2, the map drew the inventory's point, and an organisation only
the inventory named stayed public. Every served surface is checked here: list, detail, Sources,
RSS, map, events, CSV export, bulk, interconnection-point totals, and the organisation routes;
and the tier rule (a Pro caller still reads an `api_only` source).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_event,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import InterconnectionPoint, OrganizationAlias, Proposal, ProposalSource, Source
from services.ids import public_id
from tests.conftest import login, make_account, make_api_key, make_user

UTC = dt.UTC
QUEUE = "us.test.queue"
INVENTORY = "us.test.inventory"

#: What each register states about the one project.
QUEUE_SAYS: dict[str, Any] = {
    "kind": "generation",
    "name_canonical": "Blue Jay Solar",
    "technology": "solar",
    "technology_raw": "Solar - Photovoltaic Solar",
    "capacity_mw": 141.05,
    "jurisdiction": "US-TX",
    "iso": "ERCOT",
    "lifecycle_state": "built",
    "status_raw": "Completed",
    "proposed_online_date": "2025-09-30",
}
INVENTORY_SAYS: dict[str, Any] = {
    "kind": "generation",
    "name_canonical": "Blue Jay Solar I, LLC",
    "technology": "solar",
    "technology_raw": "Solar Photovoltaic",
    "capacity_mw": 210.0,
    "jurisdiction": "US-TX",
    "iso": "ERCOT",
    "lifecycle_state": "under_construction",
    "status_raw": "(TS) Construction complete, but not yet in commercial operation",
    "proposed_online_date": "2026-09-01",
}
INVENTORY_IDS = {"eia_plant_id": "64672", "eia_generator_id": "BLUJS"}
INVENTORY_ONLY_ORG = "Dimension Energy LLC"
#: Strings only the inventory states: none may appear on any public surface once it is hidden.
INVENTORY_ONLY = ("Blue Jay Solar I, LLC", "210.0", "64672", "BLUJS", "Construction complete", INVENTORY)


@pytest.fixture(autouse=True)
def _export_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))


def _alias(db: Session, org: Any, source: Source) -> None:
    db.add(
        OrganizationAlias(
            organization_id=org.id,
            alias=org.name_canonical,
            alias_normalised=org.name_canonical.lower(),
            kind="filing_spelling",
            source_id=source.id,
            source_url=source.url,
            retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
            licence_id=source.licence_id,
        )
    )
    db.flush()


def seed(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    queue = make_public_source(db, lic, id_=QUEUE)
    inventory = make_public_source(db, lic, id_=INVENTORY)
    sponsor = make_org(db, "Blue Jay Solar Holdings LLC")
    _alias(db, sponsor, queue)
    inventory_org = make_org(db, INVENTORY_ONLY_ORG)
    _alias(db, inventory_org, inventory)
    point_loc = make_location(
        db, inventory, lic, geom=(-96.06983, 30.705329), precision="exact", county_name="Grimes"
    )
    prop = make_visible_proposal(db, queue, public_id_suffix="7", sponsor=sponsor, location=point_loc)
    for name, value in INVENTORY_SAYS.items():
        setattr(prop, name, dt.date.fromisoformat(value) if name == "proposed_online_date" else value)
    prop.identifiers = dict(INVENTORY_IDS)
    prop.field_provenance = {
        name: {"source_id": INVENTORY, "licence_id": lic.id, "retrieved_at": "2026-09-27T15:15:28+00:00"}
        for name in [*INVENTORY_SAYS, "identifiers"]
    }
    prop.source_count = 2
    queue_link = next(link for link in prop.sources if link.source_id == QUEUE)
    queue_link.normalised = dict(QUEUE_SAYS)
    now = dt.datetime.now(UTC)
    db.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=INVENTORY,
            source_record_id="64672:BLUJS",
            source_url="https://example.org/inventory#64672",
            retrieved_at=now,
            licence_id=lic.id,
            raw={"Plant ID": 64672},
            normalised=dict(INVENTORY_SAYS),
            first_seen=now - dt.timedelta(days=3),
            last_seen=now,
        )
    )
    point = InterconnectionPoint(
        public_id="",
        operator="ERCOT",
        name_display="4 Iola 138kV",
        name_key="sub:iola|138|b4",
        key_rule="test",
        kind="substation",
        source_id=queue.id,
        source_url=queue.url,
        retrieved_at=now,
        licence_id=lic.id,
    )
    db.add(point)
    db.flush()
    point.public_id = public_id("poi", point.id)
    prop.interconnection_point_id = point.id
    event = make_event(db, prop, queue, event_type="status_change")
    db.commit()
    return {
        "prop": prop,
        "queue": queue,
        "inventory": inventory,
        "org": inventory_org,
        "point": point,
        "event": event,
    }


def _hide(db: Session, state: str = "ingest_only") -> None:
    db.get(Source, INVENTORY).publish_state = state  # type: ignore[union-attr]
    db.commit()


def _assert_no_inventory_value(payload: Any, where: str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    for secret in INVENTORY_ONLY:
        assert secret not in text, f"{where} still prints {secret!r}"


def _assert_queue_values(data: dict[str, Any]) -> None:
    for name in ("name_canonical", "capacity_mw", "lifecycle_state", "status_raw", "technology_raw"):
        assert data[name] == QUEUE_SAYS[name], name
    assert data["proposed_online_date"] == QUEUE_SAYS["proposed_online_date"]
    assert data["identifiers"] == {}
    assert data["source_count"] == 1
    assert data["location"] is None
    assert [p["source_id"] for p in data["provenance"]] == [QUEUE]


# ------------------------------------------------------------------------------- the API record
def test_a_visible_merged_record_serves_its_hidden_sources_values_until_the_source_is_unpublished(
    client: TestClient, db: Session
) -> None:
    """The control: while both sources are public the stored (inventory) values are served."""
    seeded = seed(db)
    data = client.get(f"/v1/proposals/{seeded['prop'].public_id}").json()["data"]
    assert data["name_canonical"] == INVENTORY_SAYS["name_canonical"]
    assert data["source_count"] == 2
    assert data["location"]["provenance"]["source_id"] == INVENTORY


def test_unpublishing_a_source_withholds_every_field_it_supplied_on_the_record_routes(
    client: TestClient, db: Session
) -> None:
    seeded = seed(db)
    pid = seeded["prop"].public_id
    _hide(db)

    detail = client.get(f"/v1/proposals/{pid}").json()
    _assert_queue_values(detail["data"])
    _assert_no_inventory_value(detail, "detail")
    assert [s["source_id"] for s in detail["licence_summary"]["sources"]] == [QUEUE]

    listed = client.get("/v1/proposals", params={"source_id": QUEUE}).json()
    row = next(r for r in listed["data"] if r["public_id"] == pid)
    _assert_queue_values(row)
    _assert_no_inventory_value(listed, "list")

    sources = client.get(f"/v1/proposals/{pid}/sources").json()
    _assert_no_inventory_value(sources, "sources panel")

    events = client.get(f"/v1/proposals/{pid}/events").json()
    assert [e["subject"]["name"] for e in events["data"]] == [QUEUE_SAYS["name_canonical"]]
    by_id = client.get(f"/v1/events/{events['data'][0]['id']}").json()
    assert by_id["data"]["subject"]["name"] == QUEUE_SAYS["name_canonical"]
    global_feed = client.get("/v1/events").json()
    _assert_no_inventory_value(global_feed, "events")


def test_the_rss_items_and_the_map_carry_no_hidden_value(client: TestClient, db: Session) -> None:
    seed(db)
    _hide(db)
    rss = client.get("/feeds/proposals.rss")
    assert rss.status_code == 200
    assert QUEUE_SAYS["name_canonical"] in rss.text
    _assert_no_inventory_value(rss.text, "proposals RSS")
    events_rss = client.get("/feeds/events.rss").text
    _assert_no_inventory_value(events_rss, "events RSS")

    geo = client.get("/v1/proposals/geo", params={"bbox": "-100,25,-90,35", "zoom": 12}).json()
    # The only placement was the inventory's exact point: unplaced now, never drawn there.
    assert geo["data"]["features"] == []
    assert geo["meta"]["unplaced_count"] == 1
    assert geo["data"]["totals"]["lifecycle_state_counts"] == {"built": 1}


def test_point_totals_sum_the_served_capacity_not_the_hidden_one(client: TestClient, db: Session) -> None:
    seeded = seed(db)
    before = client.get(f"/v1/interconnection-points/{seeded['point'].public_id}").json()["data"]
    assert before["totals"]["active_mw"] == INVENTORY_SAYS["capacity_mw"]
    _hide(db)
    after = client.get(f"/v1/interconnection-points/{seeded['point'].public_id}").json()
    assert after["data"]["totals"]["active_mw"] == 0  # the queue says built, not active
    assert after["data"]["totals"]["built_mw"] == QUEUE_SAYS["capacity_mw"]
    assert after["data"]["proposals"][0]["name_canonical"] == QUEUE_SAYS["name_canonical"]
    _assert_no_inventory_value(after, "point detail")
    embed = client.get(f"/v1/proposals/{seeded['prop'].public_id}").json()["data"]["interconnection_point"]
    assert embed["active_mw"] == 0


def test_the_csv_export_and_the_bulk_stream_carry_no_hidden_value(client: TestClient, db: Session) -> None:
    seeded = seed(db)
    _hide(db)
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()

    bulk = client.get("/v1/bulk/proposals", headers={"Authorization": f"Bearer {secret}"})
    assert bulk.status_code == 200, bulk.text
    lines = [json.loads(line) for line in bulk.iter_lines() if line]
    record = next(r for r in lines[1:] if r["public_id"] == seeded["prop"].public_id)
    _assert_queue_values(record)
    _assert_no_inventory_value(lines, "bulk")

    login(client, db, user)
    created = client.post("/v1/exports", json={"entity": "proposal", "query": {}})
    assert created.status_code == 202, created.text
    text = client.get(f"/v1/exports/{created.json()['data']['export_id']}/download").text
    _assert_no_inventory_value(text, "CSV export")
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    row = next(r for r in csv.DictReader(io.StringIO(body)) if r["public_id"] == seeded["prop"].public_id)
    assert (row["name_canonical"], row["capacity_mw"], row["source_count"]) == (
        "Blue Jay Solar",
        "141.05",
        "1",
    )
    assert row["latitude"] == "" and row["longitude"] == ""


def test_an_organisation_only_the_hidden_source_names_is_withdrawn_with_it(
    client: TestClient, db: Session
) -> None:
    seeded = seed(db)
    org_id = seeded["org"].public_id
    assert client.get(f"/v1/organizations/{org_id}").status_code == 200
    _hide(db)
    assert client.get(f"/v1/organizations/{org_id}").status_code == 404
    found = client.get("/v1/organizations", params={"q": "Dimension"}).json()["data"]
    assert org_id not in [o["public_id"] for o in found]
    # The sponsor the visible queue names stays.
    detail = client.get(f"/v1/proposals/{seeded['prop'].public_id}").json()["data"]
    assert detail["sponsor"]["name_canonical"] == "Blue Jay Solar Holdings LLC"


def test_an_api_only_source_is_read_by_pro_and_withheld_from_the_public_tier(
    client: TestClient, db: Session
) -> None:
    """The gate is the visibility predicate's own tier rule: `api_only` is on the Pro surface."""
    seeded = seed(db)
    _hide(db, "api_only")
    public = client.get(f"/v1/proposals/{seeded['prop'].public_id}").json()["data"]
    assert public["name_canonical"] == QUEUE_SAYS["name_canonical"]
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user)
    db.commit()
    pro = client.get(
        f"/v1/proposals/{seeded['prop'].public_id}", headers={"Authorization": f"Bearer {secret}"}
    ).json()["data"]
    assert pro["name_canonical"] == INVENTORY_SAYS["name_canonical"]
    assert pro["source_count"] == 2


def test_republishing_the_source_restores_its_values(client: TestClient, db: Session) -> None:
    seeded = seed(db)
    _hide(db)
    _hide(db, "public")
    data = client.get(f"/v1/proposals/{seeded['prop'].public_id}").json()["data"]
    assert data["name_canonical"] == INVENTORY_SAYS["name_canonical"]
    assert data["identifiers"] == INVENTORY_IDS


def test_an_admin_override_is_served_whichever_source_is_hidden(client: TestClient, db: Session) -> None:
    """A human decision (docs/21 §6.4) is not a source's value: it stays when the source that
    supplied the field it replaced is unpublished."""
    seeded = seed(db)
    prop = db.get(Proposal, seeded["prop"].id)
    assert prop is not None
    prop.name_canonical = "Blue Jay Solar (corrected)"
    prop.overrides = {"name_canonical": {"value": "Blue Jay Solar (corrected)", "event_id": "evt_x"}}
    db.commit()
    _hide(db)
    data = client.get(f"/v1/proposals/{seeded['prop'].public_id}").json()["data"]
    assert data["name_canonical"] == "Blue Jay Solar (corrected)"
    assert data["capacity_mw"] == QUEUE_SAYS["capacity_mw"]


def test_the_admin_view_still_shows_the_stored_row(client: TestClient, db: Session) -> None:
    """Admin reads bypass the predicate (docs/21 §5.4): the operator sees what is stored."""
    from services.api.serialize import serialize_proposal

    seeded = seed(db)
    _hide(db)
    prop = db.get(Proposal, seeded["prop"].id)
    assert prop is not None
    data = serialize_proposal(prop, sources=[s for s in prop.sources if s.active], admin=True)
    assert data["name_canonical"] == INVENTORY_SAYS["name_canonical"]
    assert data["source_count"] == 2
