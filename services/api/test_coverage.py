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


# ------------------------------------------------------------ sources per asset type (docs/24)
def _seed_ethanol(db: Session, *, second_source: bool = True) -> None:
    """The measured shape in miniature: an Atlas row with coordinates and a capacity-report row
    without, for the same plant, kept apart by `(source_id, source_asset_id)`."""
    from services.api.conftest import make_asset

    lic = make_open_licence(db)
    atlas = make_public_source(db, lic, id_="us.eia.atlas.ethanol_plants")
    make_asset(db, atlas, lic, source_asset_id="NE-poet-fairmont", asset_type="ethanol_plant", name="Poet")
    if second_source:
        report = make_public_source(db, lic, id_="us.eia.ethanol_capacity")
        make_asset(
            db,
            report,
            lic,
            source_asset_id="NE-flint-hills-fairmont",
            asset_type="ethanol_plant",
            name="Flint Hills Fairmont",
            geom=None,
        )
    db.commit()


def test_coverage_states_sources_and_rows_per_asset_type(client: TestClient, db: Session) -> None:
    _seed_ethanol(db)
    assets = client.get("/v1/coverage").json()["data"]["assets"]
    ethanol = assets["by_type"]["ethanol_plant"]
    assert ethanol["rows"] == 2
    assert ethanol["located"] == 1, "only the Atlas row carries coordinates; the page must be able to say so"
    assert ethanol["source_count"] == 2
    assert ethanol["sources"] == {
        "us.eia.atlas.ethanol_plants": {"rows": 1, "located": 1},
        "us.eia.ethanol_capacity": {"rows": 1, "located": 0},
    }
    assert ethanol["resolved"] is False
    assert assets["unresolved_multi_source"] == ["ethanol_plant"]


def test_the_resolution_mechanism_is_read_from_the_schema_not_asserted(
    client: TestClient, db: Session
) -> None:
    """`resolution.exists` is the inspector's answer: the `asset_source` link table (docs/24 §5(a))
    has landed, so every store now answers True here -- but that is a statement about the
    *mechanism*, not about any particular type being resolved with it (`resolved_asset_types`
    is the honest per-type answer, covered by the tests below)."""
    _seed_ethanol(db)
    resolution = client.get("/v1/coverage").json()["data"]["assets"]["resolution"]
    assert resolution == {"exists": True, "mechanism": "asset_source"}


def test_the_ethanol_note_is_returned_while_two_sources_are_unresolved(
    client: TestClient, db: Session
) -> None:
    _seed_ethanol(db)
    notes = {n["id"]: n for n in client.get("/v1/coverage").json()["data"]["notes"]}
    note = notes["ethanol_two_sources"]
    assert note["applies_to"] == {"unresolved_asset_type": "ethanol_plant"}
    # The measured estimate and its provenance travel with the note, typed for JSON.
    assert note["figures"]["distinct_estimate"] == 201
    assert note["figures"]["measured"] == "2026-09-22"
    assert note["figures"]["reference"] == "docs/24-asset-identity-and-provenance.md"
    assert note["figures"]["matcher_precision"] == "183/187"
    assert note["figures"]["redundant_share_floor"] == 0.466
    assert "not yet resolved" in note["headline"]
    assert "Energy Atlas name is the older one" in note["body"]


def test_the_public_text_describes_a_fusion_state_and_never_calls_it_a_bug(
    client: TestClient, db: Session
) -> None:
    _seed_ethanol(db)
    notes = client.get("/v1/coverage").json()["data"]["notes"]
    for note in notes:
        if (note["applies_to"] or {}).get("unresolved_asset_type"):
            text = f"{note['headline']} {note['body']}".lower()
            assert "duplicate" not in text and "bug" not in text, note["id"]


def test_the_ethanol_note_retires_when_the_type_has_one_source(client: TestClient, db: Session) -> None:
    """Retirement condition one: the second source goes away (or never loaded). The type is
    then one source, the row count is the asset count, and the note would contradict it."""
    _seed_ethanol(db, second_source=False)
    data = client.get("/v1/coverage").json()["data"]
    assert data["assets"]["by_type"]["ethanol_plant"]["source_count"] == 1
    assert data["assets"]["unresolved_multi_source"] == []
    assert not any(n["id"] == "ethanol_two_sources" for n in data["notes"])


def test_the_ethanol_note_retires_when_a_resolution_layer_exists(client: TestClient, db: Session) -> None:
    """Retirement condition two: something actually writes `asset_source` rows for this type. The
    `asset_source` table now exists in every store's schema (docs/24 §5(a) landed), so this
    exercises the honest half of the claim: the note stays up until a row names this *type*, not
    merely until the table exists (`services/api/coverage.py::resolved_asset_types`)."""
    from services.api.conftest import make_asset, make_asset_source

    lic = make_open_licence(db)
    atlas = make_public_source(db, lic, id_="us.eia.atlas.ethanol_plants")
    report = make_public_source(db, lic, id_="us.eia.ethanol_capacity")
    plant = make_asset(
        db, atlas, lic, source_asset_id="NE-poet-fairmont", asset_type="ethanol_plant", name="Poet"
    )
    plant2 = make_asset(
        db,
        report,
        lic,
        source_asset_id="NE-flint-hills-fairmont",
        asset_type="ethanol_plant",
        name="Flint Hills Fairmont",
        geom=None,
    )
    db.commit()

    before = client.get("/v1/coverage").json()["data"]
    assert before["assets"]["resolution"]["exists"] is True, "the mechanism already exists"
    assert before["assets"]["by_type"]["ethanol_plant"]["source_count"] == 2
    assert before["assets"]["by_type"]["ethanol_plant"]["resolved"] is False, "nothing has linked it yet"
    assert any(n["id"] == "ethanol_two_sources" for n in before["notes"]), "precondition: the note applies"

    # Simulate the resolution pass having run: both raw rows are still here (this test is not
    # exercising the merge itself, `services/ingest/assets.py::load_ethanol_plants` does that), but
    # `asset_source` now names both sources for this type, which is the honest thing
    # `resolved_asset_types` checks for.
    make_asset_source(db, plant, atlas, lic, is_primary=True, match_method="deterministic_key")
    make_asset_source(db, plant2, report, lic, is_primary=False, match_method="rule", match_score=0.767)
    db.commit()

    after = client.get("/v1/coverage").json()["data"]
    assert after["assets"]["by_type"]["ethanol_plant"]["source_count"] == 2, "the sources did not change"
    assert after["assets"]["by_type"]["ethanol_plant"]["resolved"] is True
    assert after["assets"]["unresolved_multi_source"] == []
    assert not any(n["id"] == "ethanol_two_sources" for n in after["notes"])


def test_a_second_multi_source_type_stays_unresolved_until_it_too_has_links(
    client: TestClient, db: Session
) -> None:
    """The mechanism existing for `ethanol_plant` must not silently mark `rng_project` (or any
    other multi-source type) resolved -- `resolved_asset_types` is per type, not a single flag off
    the table's existence (this is the "honestly" requirement docs/24 §5(a) names)."""
    from services.api.conftest import make_asset, make_asset_source

    lic = make_open_licence(db)
    atlas = make_public_source(db, lic, id_="us.eia.atlas.ethanol_plants")
    report = make_public_source(db, lic, id_="us.eia.ethanol_capacity")
    plant = make_asset(db, atlas, lic, source_asset_id="NE-poet-fairmont", asset_type="ethanol_plant")
    make_asset_source(db, plant, atlas, lic, is_primary=True)
    make_asset_source(db, plant, report, lic, source_record_id="NE-flint-hills-fairmont", is_primary=False)

    lmop = make_public_source(db, lic, id_="us.epa.lmop")
    agstar = make_public_source(db, lic, id_="us.epa.agstar")
    make_asset(db, lmop, lic, source_asset_id="lf-1", asset_type="rng_project", name="Landfill 1")
    make_asset(db, agstar, lic, source_asset_id="ag-1", asset_type="rng_project", name="Digester 1")
    db.commit()

    assets = client.get("/v1/coverage").json()["data"]["assets"]
    assert assets["by_type"]["ethanol_plant"]["resolved"] is True
    assert assets["by_type"]["rng_project"]["resolved"] is False
    assert assets["unresolved_multi_source"] == ["rng_project"]


def test_a_single_source_type_is_stated_as_resolvable_by_construction(
    client: TestClient, db: Session
) -> None:
    """Five of the seven live types have one source (docs/24 §0): cross-source over-counting is
    impossible there, and the statement says one source rather than implying a problem."""
    from services.api.conftest import make_asset

    lic = make_open_licence(db)
    source = make_public_source(db, lic, id_="us.eia.860m")
    make_asset(db, source, lic, source_asset_id="1", asset_type="power_plant")
    make_asset(db, source, lic, source_asset_id="2", asset_type="power_plant", name="Other")
    db.commit()
    assets = client.get("/v1/coverage").json()["data"]["assets"]
    assert assets["by_type"]["power_plant"]["source_count"] == 1
    assert assets["by_type"]["power_plant"]["rows"] == 2
    assert "power_plant" not in assets["unresolved_multi_source"]


def test_an_empty_store_states_no_asset_types(client: TestClient, db: Session) -> None:
    assets = client.get("/v1/coverage").json()["data"]["assets"]
    assert assets["by_type"] == {}
    assert assets["unresolved_multi_source"] == []
    # The mechanism exists in the schema (docs/24 §5(a) landed) even with no rows to resolve.
    assert assets["resolution"]["exists"] is True
