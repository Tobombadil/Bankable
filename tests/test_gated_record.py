"""`services/api/visibility.py::GatedRecord`, the served view of one record, rule by rule
(docs/21 §8 field gate; the per-surface behaviour is in `tests/test_source_unpublish_fields.py`).
These pin the corners the surface tests do not reach: an opportunity's typed fallback, the
`select_basis` filter, a shape narrowed by `link_ok`, a record with no readable link, and the
organisation arm on a row that was never stored.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from services.api.conftest import (
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.visibility import gated_opportunity, gated_proposal, gated_record, organization_visible
from services.db.models import Opportunity, OpportunitySource, Organization, Source

UTC = dt.UTC


def _two_source_opportunity(db: Session) -> tuple[Opportunity, Source, Source]:
    lic = make_open_licence(db)
    shown = make_public_source(db, lic, id_="us.test.shown")
    held = make_public_source(db, lic, id_="us.test.held")
    opp = make_visible_opportunity(db, shown, public_id_suffix="1")
    shown_link = opp.sources[0]
    shown_link.normalised = {
        "title": "Shown RFP",
        "due_at": "2026-12-01T17:00:00+00:00",
        "open_at": "2026-10-01",
    }
    db.add(
        OpportunitySource(
            opportunity_id=opp.id,
            source_id=held.id,
            source_record_id="H1",
            source_url="https://example.org/held/1",
            retrieved_at=dt.datetime.now(UTC),
            licence_id=lic.id,
            normalised={"title": "Held RFP"},
            first_seen=dt.datetime.now(UTC),
            last_seen=dt.datetime.now(UTC),
        )
    )
    opp.title = "Held RFP"
    opp.due_at = dt.datetime(2027, 1, 1, tzinfo=UTC)
    opp.open_at = dt.date(2026, 11, 1)
    opp.field_provenance = {
        f: {"source_id": held.id, "licence_id": lic.id, "retrieved_at": "2026-09-30"}
        for f in ("title", "due_at", "open_at", "summary")
    }
    opp.source_count = 2
    db.flush()
    db.expire(opp, ["sources"])  # the view-only collection was loaded before the second link
    return opp, shown, held


def test_an_opportunitys_fallback_restores_dates_and_instants_from_the_readable_link(db: Session) -> None:
    opp, _shown, held = _two_source_opportunity(db)
    held.publish_state = "ingest_only"
    db.flush()
    view = gated_opportunity(opp)
    assert view.title == "Shown RFP"
    assert view.due_at == dt.datetime(2026, 12, 1, 17, tzinfo=UTC)
    assert view.open_at == dt.date(2026, 10, 1)
    assert view.summary is None  # no readable link states one
    assert view.source_count == 1
    assert gated_record(view) is view  # idempotent on a view


def test_a_field_with_no_provenance_is_rederived_once_any_link_is_hidden(db: Session) -> None:
    opp, _shown, held = _two_source_opportunity(db)
    opp.status = "awarded"  # stored, no provenance recorded for it
    held.publish_state = "ingest_only"
    db.flush()
    assert gated_opportunity(opp).status == "unknown"  # the shown link states none: placeholder


def test_select_basis_entries_of_a_hidden_source_are_dropped_and_the_key_with_them(db: Session) -> None:
    lic = make_open_licence(db)
    shown = make_public_source(db, lic, id_="us.test.shown")
    held = make_public_source(db, lic, id_="us.test.held")
    held.publish_state = "ingest_only"
    prop = make_visible_proposal(db, shown)
    prop.identifiers = {
        "queue_ids": [{"iso": "ERCOT", "id": "Q1"}],
        "select_basis": {shown.id: {"reason": "named"}, held.id: {"reason": "flagged"}},
    }
    prop.field_provenance = {
        "identifiers": {"source_id": shown.id, "licence_id": lic.id, "retrieved_at": "x"}
    }
    db.flush()
    ids = gated_proposal(prop).identifiers
    assert ids["select_basis"] == {shown.id: {"reason": "named"}}
    prop.identifiers = {"select_basis": {held.id: {"reason": "flagged"}}}
    db.flush()
    assert gated_proposal(prop).identifiers == {}


def test_link_ok_narrows_the_view_to_sources_whose_licence_permits_the_shape(db: Session) -> None:
    """Bulk and export narrow by licence flag: a provenance source the tier reads but whose licence
    does not permit the shape supplies nothing, even when it is not one of the record's links."""
    lic = make_open_licence(db)
    shown = make_public_source(db, lic, id_="us.test.shown")
    elsewhere = make_public_source(db, lic, id_="us.test.elsewhere")
    prop = make_visible_proposal(db, shown)
    prop.sources[0].normalised = {"name_canonical": "Shown Spelling"}
    prop.name_canonical = "Elsewhere Spelling"
    prop.field_provenance = {
        "name_canonical": {"source_id": elsewhere.id, "licence_id": lic.id, "retrieved_at": "x"}
    }
    db.flush()
    assert gated_proposal(prop).name_canonical == "Elsewhere Spelling"
    narrowed = gated_proposal(prop, "public", lambda source: source.id != elsewhere.id)
    assert narrowed.name_canonical == "Shown Spelling"
    assert gated_proposal(prop, "public", lambda source: source.id == elsewhere.id).location is None


def test_a_record_with_no_readable_link_shows_no_raw_value(db: Session) -> None:
    lic = make_open_licence(db)
    held = make_public_source(db, lic, id_="us.test.held")
    prop = make_visible_proposal(db, held)
    prop.status_raw = "Active"
    held.publish_state = "ingest_only"
    db.flush()
    view = gated_proposal(prop)
    assert view.status_raw is None
    assert view.raw_withheld() == {}  # no source to name: nothing readable supplied it


def test_an_organisation_never_stored_is_judged_by_its_state_alone() -> None:
    org = Organization(
        public_id="org_x", slug="x", name_canonical="X", name_normalised="x", type="other", country="US"
    )
    org.publish_state = "public"
    assert organization_visible(org)


def test_a_curated_issuer_stays_visible_whatever_its_aliases_say(db: Session) -> None:
    from services.db.models import OrganizationAlias

    lic = make_open_licence(db)
    held = make_public_source(db, lic, id_="us.test.held")
    held.publish_state = "ingest_only"
    org = make_org(db, "Curated Utility Co")
    db.add(
        OrganizationAlias(
            organization_id=org.id,
            alias="Curated Utility Co",
            alias_normalised="curated utility co",
            kind="filing_spelling",
            source_id=held.id,
            source_url=held.url,
            retrieved_at=dt.datetime.now(UTC),
            licence_id=lic.id,
        )
    )
    db.flush()
    assert not organization_visible(org)
    org.is_curated_issuer = True
    db.flush()
    assert organization_visible(org)
