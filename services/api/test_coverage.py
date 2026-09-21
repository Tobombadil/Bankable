"""`GET /v1/coverage`, `GET /v1/lifecycle-states`, and the release on `/v1/sources` and health.

The through-line: an absence must read as a fact about our coverage, and a fetch date must never
read as the data's age.
"""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)

UTC = dt.UTC


def _seed(db: Session) -> None:
    lic = make_open_licence(db)
    source = make_public_source(db, lic, id_="us.eia.860m")
    source.vintage = "2026-07"
    source.vintage_basis = "artefact_filename"
    source.last_success_at = dt.datetime(2026, 9, 13, tzinfo=UTC)
    queue = make_public_source(db, lic, id_="us.iso.caiso.gen_queue")
    queue.vintage = None
    queue.vintage_basis = "not_stated"
    make_visible_proposal(db, source, public_id_suffix="1", technology="solar")
    make_visible_proposal(db, queue, public_id_suffix="2", technology="wind")
    make_org(db)
    db.commit()


# ------------------------------------------------------------------ release versus fetch date


def test_source_carries_the_release_the_source_states_not_the_date_we_fetched(
    client: TestClient, db: Session
) -> None:
    _seed(db)
    rows = {s["source_id"]: s for s in client.get("/v1/sources").json()["data"]}
    eia = rows["us.eia.860m"]
    assert eia["vintage"] == {
        "value": "2026-07",
        "label": "July 2026",
        "basis": "artefact_filename",
        "stated": True,
    }
    # The fetch date is still reported, and is a different, later date. That gap is the bug.
    assert eia["last_success_at"].startswith("2026-09-13")


def test_a_source_that_states_no_release_says_so_rather_than_borrowing_the_fetch_date(
    client: TestClient, db: Session
) -> None:
    _seed(db)
    rows = {s["source_id"]: s for s in client.get("/v1/sources").json()["data"]}
    queue = rows["us.iso.caiso.gen_queue"]["vintage"]
    assert queue["stated"] is False
    assert queue["value"] is None
    assert queue["basis"] == "not_stated"


def test_a_source_no_load_has_examined_is_undetermined_not_not_stated(
    client: TestClient, db: Session
) -> None:
    lic = make_open_licence(db)
    make_public_source(db, lic, id_="us.fresh")
    db.commit()
    row = client.get("/v1/sources/us.fresh").json()["data"]
    assert row["vintage"]["basis"] == "undetermined"
    assert row["vintage"]["stated"] is False


def test_health_reports_the_oldest_stated_release_beside_the_newest_fetch(
    client: TestClient, db: Session
) -> None:
    _seed(db)
    health = client.get("/v1/health").json()
    assert health["source_vintage"]["oldest"] == "2026-07"
    assert health["source_vintage"]["oldest_label"] == "July 2026"
    assert health["source_vintage"]["oldest_source_id"] == "us.eia.860m"
    assert health["source_vintage"]["sources_stating_a_release"] == 1
    assert health["source_vintage"]["sources_stating_none"] == 1
    # Both dates are present and neither replaces the other.
    assert health["source_data_as_of"] is not None


def test_health_states_no_release_rather_than_a_date_when_no_source_states_one(
    client: TestClient, db: Session
) -> None:
    lic = make_open_licence(db)
    source = make_public_source(db, lic, id_="us.iso.caiso.gen_queue")
    source.vintage_basis = "not_stated"
    make_visible_proposal(db, source)
    db.commit()
    vintage = client.get("/v1/health").json()["source_vintage"]
    assert vintage["oldest"] is None
    assert vintage["sources_stating_none"] == 1


# ---------------------------------------------------------------------------------- coverage


def test_coverage_names_the_technologies_no_source_produces(client: TestClient, db: Session) -> None:
    _seed(db)
    data = client.get("/v1/coverage").json()["data"]
    assert "load" in data["technologies"]["absent"], (
        "the register carries no large-load or data-centre proposals, and the page must say so"
    )
    assert "solar" not in data["technologies"]["absent"]
    assert "load" in data["technologies"]["vocabulary"]


def test_coverage_lists_the_withheld_supply_registers_with_a_reason(client: TestClient, db: Session) -> None:
    _seed(db)
    withheld = client.get("/v1/coverage").json()["data"]["sources"]["withheld"]
    by_id = {s["source_id"]: s for s in withheld}
    for source_id in (
        "us.iso.pjm.gen_queue",
        "us.iso.miso.gen_queue",
        "us.iso.spp.gen_queue",
        "us.iso.isone.gen_queue",
    ):
        assert source_id in by_id, source_id
        assert by_id[source_id]["supply"] is True
        assert by_id[source_id]["reason"]
    # Supply registers sort first: they are the absence a reader of a map feels.
    assert withheld[0]["supply"] is True


def test_coverage_excludes_channels_and_aggregators_from_the_withheld_list(
    client: TestClient, db: Session
) -> None:
    """Our own social accounts and the private aggregators we refuse on principle are not
    "withheld pending a licence"; listing them would pad the statement and imply a licence could
    change it."""
    _seed(db)
    withheld = client.get("/v1/coverage").json()["data"]["sources"]["withheld"]
    categories = {s["category"] for s in withheld}
    assert not categories & {"social_channel", "news", "aggregator"}


def test_coverage_counts_ownership_depth(client: TestClient, db: Session) -> None:
    _seed(db)
    ownership = client.get("/v1/coverage").json()["data"]["ownership"]
    assert ownership["organizations"] == 1
    assert ownership["with_recorded_parent"] == 0
    assert ownership["without_recorded_parent"] == 1


def test_coverage_notes_carry_the_date_they_were_written(client: TestClient, db: Session) -> None:
    _seed(db)
    notes = client.get("/v1/coverage").json()["data"]["notes"]
    assert notes
    assert all(n["written"] for n in notes)
    assert any(n["id"] == "no_large_load" for n in notes)


def test_a_note_is_dropped_when_the_fact_it_explains_stops_being_true(
    client: TestClient, db: Session
) -> None:
    """The prose can never contradict the measurement printed beside it: the large-load note is
    keyed to `load` being an absent technology, so a single load row retires it."""
    lic = make_open_licence(db)
    source = make_public_source(db, lic)
    make_visible_proposal(db, source, technology="load")
    db.commit()
    data = client.get("/v1/coverage").json()["data"]
    assert "load" not in data["technologies"]["absent"]
    assert not any(n["id"] == "no_large_load" for n in data["notes"])


def test_coverage_reports_source_counts_from_the_registry_and_the_store(
    client: TestClient, db: Session
) -> None:
    _seed(db)
    sources = client.get("/v1/coverage").json()["data"]["sources"]
    assert sources["registered"] > sources["with_rows"]
    assert set(sources["loaded_source_ids"]) == {"us.eia.860m", "us.iso.caiso.gen_queue"}


# -------------------------------------------------------------------------- status definitions


def test_lifecycle_states_publishes_a_definition_and_its_mappings(client: TestClient) -> None:
    data = client.get("/v1/lifecycle-states").json()["data"]
    states = {s["state"]: s for s in data["lifecycle_states"]}
    studied = states["studied"]
    assert studied["definition"]
    assert studied["uncertainty"], "the broadest state has to say where it is a judgement"
    raws = {(m["status_key"], m["raw"]) for m in studied["maps_from"]}
    assert ("caiso", "ACTIVE") in raws, "the raw value shown is the raw value the pipeline keys off"


def test_lifecycle_states_names_the_four_pre_construction_states(client: TestClient) -> None:
    data = client.get("/v1/lifecycle-states").json()["data"]
    assert data["pre_construction_states"] == ["filed", "studied", "permitted", "contracted"]


def test_lifecycle_states_says_when_the_prose_was_written(client: TestClient) -> None:
    data = client.get("/v1/lifecycle-states").json()["data"]
    assert data["definitions_written"]
    assert data["status_map_files"], "and where the derived half came from"


def test_lifecycle_states_covers_both_vocabularies(client: TestClient) -> None:
    from services.db.models import LIFECYCLE_STATES, OPPORTUNITY_STATUSES

    data = client.get("/v1/lifecycle-states").json()["data"]
    assert [s["state"] for s in data["lifecycle_states"]] == list(LIFECYCLE_STATES)
    assert [s["state"] for s in data["opportunity_statuses"]] == list(OPPORTUNITY_STATUSES)
