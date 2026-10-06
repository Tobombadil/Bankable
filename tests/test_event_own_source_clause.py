"""The event predicate's own-source clause (`services/api/visibility.py::event_visibility_filter`,
`Source.publish_state` against the tier's permitted states).

QA's mutation check of 2026-09-30 (M6) removed exactly that clause and the whole repository suite
still passed: no test held an event whose *own* source is off the public surface while its licence
is publishable and its subject is visible through another source -- docs/21 §8 item 2's
mixed-provenance event. This is that test. The licence is `open` on purpose, so the licence clause
(M5) cannot be what hides the event, and the subject is public through a second, public source, so
the subject clause (M15) cannot be either: only the own-source clause stands between the event and
every surface below.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.ids import public_id
from tests.conftest import make_account, make_api_key, make_user


def _seed(db: Session, state: str) -> tuple[str, str, str]:
    lic = make_open_licence(db)
    shown = make_public_source(db, lic, id_="us.test.shown")
    held = make_public_source(db, lic, id_="us.test.held")
    held.publish_state = state
    prop = make_visible_proposal(db, shown, public_id_suffix="1")
    event = make_event(db, prop, held, event_type="status_change")
    db.commit()
    return prop.public_id, public_id("evt", event.id), held.id


def test_an_event_from_a_source_off_the_public_surface_is_on_no_public_route(
    client: TestClient, db: Session
) -> None:
    prop_id, event_id, held = _seed(db, "ingest_only")
    assert client.get(f"/v1/proposals/{prop_id}").status_code == 200  # the subject is public

    assert client.get("/v1/events").json()["data"] == []
    assert client.get(f"/v1/events/{event_id}").status_code == 404
    assert client.get(f"/v1/proposals/{prop_id}/events").json()["data"] == []
    for feed in ("/feeds/events.rss", "/feeds/events.json"):
        body = client.get(feed).text
        assert held not in body and event_id not in body, feed


def test_an_api_only_sources_event_is_pro_only(client: TestClient, db: Session) -> None:
    """The same clause, per tier: `api_only` is on the Pro surface and off the public one."""
    prop_id, event_id, _held = _seed(db, "api_only")
    assert client.get(f"/v1/events/{event_id}").status_code == 404
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user)
    db.commit()
    pro = client.get(f"/v1/events/{event_id}", headers={"Authorization": f"Bearer {secret}"})
    assert pro.status_code == 200
    assert pro.json()["data"]["subject"]["public_id"] == prop_id
