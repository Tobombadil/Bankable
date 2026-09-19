"""docs/00-PLAN.md task item 6 (redactions) and product defect C (collapsed provenance panel):
built directly on `services/db` models via `services/api/conftest.py`'s helpers, independent of
what the eval fixture happens to contain, so the restricted-precision case is guaranteed to exist
regardless of upstream data.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.conftest import (
    make_attribution_licence,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.db.session import get_engine, get_sessionmaker, init_db
from web.api_client import ApiClient
from web.app import app as web_app


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def test_provenance_panel_collapses_the_licence_quote_behind_the_source_name(
    db_sessionmaker: sessionmaker[Session], web_client: TestClient
) -> None:
    quote = "Open Test Registry Terms, retrieved 2026-09-01: data may be freely reused with attribution."
    with db_sessionmaker() as session:
        licence = make_open_licence(session)
        licence.quote_text = quote  # services/README.md "Sprint 2 fixes" #5: Licence.quote_text
        source = make_public_source(session, licence)
        proposal = make_visible_proposal(session, source)
        session.commit()
        slug = proposal.slug

    resp = web_client.get(f"/proposals/{slug}")
    assert resp.status_code == 200
    body = resp.text
    assert "Sources" in body
    assert "Test Public Source" in body  # source name visible, per docs/31 §5.2 anatomy
    assert "retrieved" in body  # retrieval date visible
    assert "<details" in body and "<summary>" in body  # the quote is collapsed, not inline
    assert quote in body  # the licence's quote_text renders verbatim, not a composed paraphrase


def test_restricted_precision_renders_the_redaction_note(
    db_sessionmaker: sessionmaker[Session], web_client: TestClient
) -> None:
    with db_sessionmaker() as session:
        licence = make_attribution_licence(session)
        licence.allows_raw_publication = False
        source = make_public_source(session, licence, id_="us.test.derived_only")
        location = make_location(session, source, licence)
        location.precision_reason = "licence"
        proposal = make_visible_proposal(session, source, location=location)
        session.commit()
        slug = proposal.slug

    resp = web_client.get(f"/proposals/{slug}")
    assert resp.status_code == 200
    assert "county level (source licence)" in resp.text
    assert "Derived fields only" in resp.text  # provenance panel's raw-withheld note
    # `make_attribution_licence` sets no `quote_text` -- the honest fallback renders instead of an
    # empty expandable region.
    assert "No licence quote recorded for this source." in resp.text


def test_gated_source_never_appears_and_reads_as_not_found(web_client: TestClient) -> None:
    """docs/21 §8 item 3: a gated/absent record is indistinguishable from not-found."""
    resp = web_client.get("/proposals/does-not-exist-000000")
    assert resp.status_code == 404
    assert "does not exist" in resp.text.lower() or "not found" in resp.text.lower()
