"""How the record page's history words a `delisted` event (owner decision 2026-10-10).

A record that left one of the four full-register queues reads "No longer in ERCOT's report (reason
not stated).", with the register's display name, and never "withdrawn". Same fake-`Transport`
pattern as `web/test_sites.py`, duplicated rather than imported (no `web/test_*.py` imports
another): `web.app.app` against canned API envelopes, no database.
"""

from __future__ import annotations

import html
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pipeline.connectors.registry import RegistrationError, Registry
from web.api_client import ApiClient
from web.app import app as web_app
from web.interconnection_points import change_label
from web.viewmodels import DELISTED_PHRASE, PROPOSAL_SOURCE_LABELS, _event_line, proposal_history

ERCOT = "us.iso.ercot.gen_queue"
#: The same literal `services.db.models.DELISTED_WORDING` is pinned to
#: (`services/ingest/test_loader_delisted.py`), with the history's closing full stop.
WORDING = "No longer in {register}'s report (reason not stated)."


def _delisted(source_id: str, register: str | None, source_name: str) -> dict[str, Any]:
    after: dict[str, Any] = {"source_id": source_id, "reason": "not stated"}
    if register is not None:
        after["register_name"] = register
    return {
        "id": "evt_1",
        "event_type": "delisted",
        "observed_at": "2026-10-09T06:00:00Z",
        "before": None,
        "after": after,
        "changed_keys": [],
        "reason": f"No longer in {register}'s report (reason not stated)" if register else None,
        "provenance": {"source_id": source_id, "source_name": source_name},
    }


def test_the_line_is_the_owner_wording() -> None:
    assert WORDING == DELISTED_PHRASE + "."


def test_the_interconnection_points_recent_changes_say_it_too() -> None:
    """ "Recent changes at this point" lists a departure (it frees queue space there) in the same
    words, as a phrase like its other rows."""
    label = change_label(_delisted(ERCOT, "ERCOT", "ERCOT GIS Report"))
    assert label == "No longer in ERCOT's report (reason not stated)"


@pytest.mark.parametrize(
    ("source_id", "register", "source_name"),
    [
        (ERCOT, "ERCOT", "ERCOT GIS Report (Generator Interconnection Status)"),
        ("us.iso.caiso.gen_queue", "CAISO", "CAISO Public Queue Report"),
        ("us.iso.nyiso.gen_queue", "NYISO", "NYISO Interconnection Queue"),
        ("gb.neso.tec_register", "NESO", "NESO Transmission Entry Capacity (TEC) Register"),
    ],
)
def test_a_delisted_event_reads_with_the_registers_display_name(
    source_id: str, register: str, source_name: str
) -> None:
    line = _event_line(_delisted(source_id, register, source_name))
    assert line == f"No longer in {register}'s report (reason not stated)."
    assert line is not None and "withdr" not in line.lower()
    # Without the name on the event (an older row), the site's own short name for the source.
    assert _event_line(_delisted(source_id, None, source_name)) == line


def test_an_unknown_source_falls_back_to_its_registered_name() -> None:
    line = _event_line(_delisted("us.iso.test.gen_queue", None, "Test ISO Queue"))
    assert line == "No longer in Test ISO Queue's report (reason not stated)."


def test_the_display_names_are_the_ones_the_site_prints_for_each_source() -> None:
    """`Connector.register_name` (what the event stores) equals `PROPOSAL_SOURCE_LABELS` (what the
    rest of the record page prints), for every connector that announces removals."""
    registry = Registry()
    announcing = {}
    for source_id in registry.ids():
        try:
            cls = registry.connector_class(source_id)
        except RegistrationError:
            continue
        if cls.announce_removals:
            announcing[source_id] = cls.register_name
    assert announcing, "no connector announces removals"
    assert announcing == {sid: PROPOSAL_SOURCE_LABELS[sid][0] for sid in announcing}


class _Api:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        assert path == "/v1/proposals/PROP_E1/events"
        return {"data": self.events}


def test_the_history_lists_it_beside_the_records_other_changes() -> None:
    events = [
        {
            "event_type": "created",
            "observed_at": "2026-09-12T06:00:00Z",
            "provenance": {"source_id": ERCOT, "source_name": "ERCOT GIS Report"},
        },
        _delisted(ERCOT, "ERCOT", "ERCOT GIS Report"),
    ]
    items = proposal_history(_Api(events), {"public_id": "PROP_E1"})  # type: ignore[arg-type]
    assert [i["text"] for i in items] == [
        "No longer in ERCOT's report (reason not stated).",
        "First published from ERCOT GIS Report.",
    ]


# ------------------------------------------------------------------------------ the rendered page
HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-10-09",
    "live_as_of": "2026-10-09T06:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}
PROV = {
    "source_id": ERCOT,
    "source_name": "ERCOT GIS Report (Generator Interconnection Status)",
    "source_url": "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1281327706",
    "retrieved_at": "2026-10-09T06:00:00Z",
    "reuse_class": "open",
    "attribution_text": None,
    "source_record_id": "23INR0001",
}
PROPOSAL: dict[str, Any] = {
    "public_id": "PROP_E1",
    "slug": "prop_e1",
    "name_canonical": "Bluebonnet Solar",
    "kind": "generation",
    "technology": "solar",
    "capacity_mw": 200.0,
    "jurisdiction": "US-TX",
    "lifecycle_state": "studied",
    "status_raw": "Active",
    "identifiers": {},
    "proposed_online_date": None,
    "schedule_slip": None,
    "source_count": 1,
    "provenance": [PROV],
}


def _list_env(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows)},
        "page": {"next_cursor": None, "has_more": False},
        "licence_summary": {"sources": []},
    }


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        return httpx.Response(202, json={})

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return self.get(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def client() -> Iterator[TestClient]:
    events = [
        {
            "event_type": "created",
            "observed_at": "2026-09-12T06:00:00Z",
            "provenance": PROV,
        },
        _delisted(ERCOT, "ERCOT", PROV["source_name"]),
    ]
    web_app.state.api_client = ApiClient(
        FakeTransport(
            {
                "/v1/health": (200, HEALTH),
                "/v1/meta/vocabularies": (200, VOCAB),
                "/v1/sources": (200, {"data": []}),
                f"/v1/sources/{ERCOT}": (
                    200,
                    {"data": {"licence": {"url": "https://example.org/l", "quote_text": "Open."}}},
                ),
                "/v1/proposals": (200, _list_env([PROPOSAL])),
                "/v1/proposals/PROP_E1": (200, {"data": PROPOSAL}),
                "/v1/proposals/PROP_E1/events": (200, _list_env(events)),
                "/v1/ui-events": (202, {}),
            }
        )
    )
    web_app.state.lag_days_default = None
    with TestClient(web_app) as c:
        yield c
    for key in ("api_client", "lag_days_default"):
        web_app.state.__dict__.pop(key, None)


def test_the_record_page_says_no_longer_in_the_report(client: TestClient) -> None:
    resp = client.get("/proposals/prop_e1")
    assert resp.status_code == 200
    page = html.unescape(resp.text)
    history = page[page.index('aria-label="History"') :]
    assert "No longer in ERCOT's report (reason not stated)." in history
    assert "First published from" in history
    assert "withdrawn" not in history.lower()
