"""The pricing coverage line is derived, so it cannot claim a queue the store does not hold (expert
review 2026-10-07: "/pricing says 'tracks every major US interconnection queue'" while PJM, MISO, SPP
and ISO-NE have no rows and `/proposals?iso=PJM` lists EIA-860M units unlabelled)."""

from __future__ import annotations

from web.coverage_statement import FALLBACK, coverage_line, manifest_categories

COVERAGE = {
    "sources": {
        "loaded_source_ids": [
            "us.iso.caiso.gen_queue",
            "us.iso.ercot.gen_queue",
            "us.iso.nyiso.gen_queue",
            "gb.neso.tec_register",
            "us.eia.860m",
            "eu.ted.api",
            "us.grants_gov.search2",
            "us.epa.echo.icis_air",
        ],
        "withheld": [
            {"source_id": "us.iso.pjm.gen_queue", "supply": True},
            {"source_id": "us.iso.miso.gen_queue", "supply": True},
            {"source_id": "us.iso.spp.gen_queue", "supply": True},
            {"source_id": "us.iso.isone.gen_queue", "supply": True},
            {"source_id": "ca.aeso.connection_list", "supply": True},
        ],
    }
}


def test_the_line_names_loaded_queues_and_the_withheld_iso_queues() -> None:
    line = coverage_line(COVERAGE)
    assert "every major" not in line
    assert "Interconnection queues with rows here: CAISO, ERCOT, NESO and NYISO." in line
    assert "The ISO-NE, MISO, PJM and SPP queues are not covered" in line
    assert "appear only as EIA-860M units" in line
    assert "Tenders and funding notices: Grants.gov and TED." in line
    assert "ICIS" not in line  # a permit register is not a queue or a tender feed


def test_a_queue_that_starts_loading_is_named_without_a_copy_change() -> None:
    data = {
        "sources": {"loaded_source_ids": ["us.iso.caiso.gen_queue", "us.iso.pjm.gen_queue"], "withheld": []}
    }
    line = coverage_line(data)
    assert "CAISO and PJM" in line and "not covered" not in line


def test_nothing_is_claimed_when_the_api_cannot_say() -> None:
    assert coverage_line(None) == FALLBACK
    assert manifest_categories()["us.iso.pjm.gen_queue"] == "generation_queue"


def test_an_iso_filter_on_an_uncovered_queue_says_the_rows_are_eia_860m() -> None:
    """Expert review 2026-10-07: `/proposals?iso=PJM` listed 382 EIA-860M units with nothing saying
    PJM's own queue is not covered."""
    from web.coverage_statement import uncovered_iso_notes

    notes = uncovered_iso_notes("PJM,ERCOT,ISONE", COVERAGE)
    assert len(notes) == 2
    assert notes[0].startswith("PJM's own interconnection queue is not covered here.")
    assert "come from EIA-860M" in notes[0]
    assert notes[1].startswith("ISO-NE's own")
    assert uncovered_iso_notes("ERCOT", COVERAGE) == []  # loaded: nothing to say
    assert uncovered_iso_notes("PJM", None) == []  # the API cannot say: claim nothing
    assert uncovered_iso_notes(None, COVERAGE) == []


def test_methodology_explains_why_there_is_no_texas_large_load_request() -> None:
    """Lane L11 / expert review 2026-10-07: the absence is explained on the coverage page, from
    `data/vocabulary/coverage_notes.yaml` through the real `/v1/coverage`."""
    from collections.abc import Iterator

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from services.api.app import app as api_app
    from services.api.deps import get_db
    from services.db.session import get_engine, get_sessionmaker, init_db
    from web.api_client import ApiClient
    from web.app import app as web_app

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    sessions = get_sessionmaker(engine)

    def _db() -> Iterator[Session]:
        with sessions() as session:
            yield session

    api_app.dependency_overrides[get_db] = _db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    try:
        with TestClient(web_app) as client:
            body = client.get("/methodology").text
    finally:
        api_app.dependency_overrides.clear()
        del web_app.state.api_client
    block = body.split('id="note-ercot_large_load_not_request_level"')[1].split("</div>")[0]
    assert "ERCOT publishes no request-level large-load data" in block
    assert "system-wide totals and charts in PDF decks" in block
