"""Rendering of the schedule-slip signal on `/proposals` and `/proposals/{slug}`
(`web/viewmodels.py::slip_display`, `_macros.html::slip_note`, docs/22 §18.5).

The behaviour these hold in place, in order of how badly each would mislead a reader:
  * a proposal with no target date renders no marker at all -- "nothing promised" must never be
    shown as "overdue";
  * the target date is printed next to the marker wherever it appears, because the main false
    positive is a register that stopped maintaining the date and a reader can only spot that if
    the date is on screen;
  * the `under_1y` bucket renders plain and the older buckets render as a warning, so a project a
    few months late does not look like a dead one.

The page half drives `web/app.py` against a fake `Transport`, the same pattern as
`web/test_nearby_relevance.py` -- duplicated rather than imported because no `web/test_*.py`
imports another (this repo's convention).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import flatten_proposal, slip_display

DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [
            {"value": "under_1y", "label": "Overdue under 1 year"},
            {"value": "1_to_3y", "label": "Overdue 1-3 years"},
            {"value": "over_3y", "label": "Overdue over 3 years"},
        ],
    }
}


# ----------------------------------------------------------------- slip_display in isolation
def test_no_slip_object_renders_nothing() -> None:
    """A proposal with no target date arrives here as `None` from the API and must stay `None`."""
    assert slip_display(None) is None


def test_flatten_proposal_without_schedule_slip() -> None:
    record = flatten_proposal(
        {
            "public_id": "prop_1",
            "slug": "p",
            "name_canonical": "P",
            "proposed_online_date": None,
            "schedule_slip": None,
        }
    )
    assert record["slip"] is None


@pytest.mark.parametrize(
    ("days", "bucket", "amount", "tone"),
    [
        (91, "under_1y", "3 months", "quiet"),
        (365, "under_1y", "12 months", "quiet"),
        (400, "1_to_3y", "1.1 years", "warn"),
        (2000, "over_3y", "5.5 years", "warn"),
    ],
)
def test_slip_display_wording_and_tone(days: int, bucket: str, amount: str, tone: str) -> None:
    out = slip_display({"days_late": days, "bucket": bucket, "target_date": "2024-01-01", "grace_days": 90})
    assert out is not None
    assert out["amount"] == amount
    assert out["label"] == f"overdue by {amount}"
    assert out["tone"] == tone
    assert "2024-01-01" in out["detail"]


def test_under_1y_is_quiet_and_older_buckets_warn() -> None:
    """The cry-wolf rule, pinned: 67 of the 129 slipped rows on the 2026-09-21 load are
    `under_1y`, and colouring all of them is how the signal stops being read."""
    from web.viewmodels import SLIP_TONE

    assert SLIP_TONE["under_1y"] == "quiet"
    assert SLIP_TONE["1_to_3y"] == SLIP_TONE["over_3y"] == "warn"


# ------------------------------------------------------------------------------- the pages
class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]] | None = None) -> None:
        self.responses: dict[str, tuple[int, Any]] = dict(responses or {})
        self.calls: list[tuple[str, str, Any]] = []

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, json))
        return self._respond(url)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append((method, url, json if json is not None else dict(params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        yield client
    web_app.state.__dict__.pop("api_client", None)
    web_app.state.__dict__.pop("lag_days_default", None)
    web_app.state.__dict__.pop("sitemap_cache", None)
    web_app.state.__dict__.pop("asset_type_counts_cache", None)


def _install(transport: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None


def _proposal(public_id: str, slip: dict[str, Any] | None, date: str | None) -> dict[str, Any]:
    return {
        "public_id": public_id,
        "slug": public_id.lower(),
        "name_canonical": f"Project {public_id}",
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 120.0,
        "jurisdiction": "US-TX",
        "lifecycle_state": "under_construction",
        "status_raw": "Under Construction",
        "identifiers": {},
        "proposed_online_date": date,
        "schedule_slip": slip,
        "source_count": 1,
        "provenance": [
            {
                "source_id": "us.iso.ercot.gen_queue",
                "source_name": "ERCOT Queue",
                "source_url": "https://example.org/q",
                "retrieved_at": "2026-09-13T00:00:00Z",
                "reuse_class": "open",
                "attribution_text": "ERCOT",
                "source_record_id": "23INR0001",
            }
        ],
    }


LATE = {"target_date": "2024-01-01", "days_late": 994, "bucket": "1_to_3y", "grace_days": 90}


def _list_env(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


def _base(rows: list[dict[str, Any]]) -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/proposals": (200, _list_env(rows)),
            "/v1/proposals/geo": (
                200,
                {"data": {"totals": {"lifecycle_state_counts": {"under_construction": len(rows)}}}},
            ),
        }
    )


def test_list_row_shows_the_marker_with_the_target_date(web_client: TestClient) -> None:
    _install(_base([_proposal("PROP_LATE", LATE, "2024-01-01")]))
    html = web_client.get("/proposals").text
    assert "slip-note" in html
    assert "overdue by 2.7 years" in html
    assert "2024-01-01" in html


def test_list_row_without_a_date_shows_no_marker(web_client: TestClient) -> None:
    """The failure this whole signal must not have: an undated proposal reading as overdue."""
    _install(_base([_proposal("PROP_UNDATED", None, None)]))
    html = web_client.get("/proposals").text
    assert "slip-note" not in html
    # "Overdue ..." still appears in the filter select's option labels; what must not appear is
    # the per-row marker.
    assert "overdue by" not in html.lower()


def test_list_offers_the_schedule_filter_and_passes_it_through(web_client: TestClient) -> None:
    transport = _base([_proposal("PROP_LATE", LATE, "2024-01-01")])
    _install(transport)
    html = web_client.get("/proposals?slip_bucket=over_3y").text
    assert 'name="slip_bucket"' in html
    assert "Overdue over 3 years" in html
    sent = [params for method, url, params in transport.calls if url == "/v1/proposals"]
    assert sent and sent[0]["slip_bucket"] == "over_3y"


def test_detail_page_states_the_derivation_and_the_caveat(web_client: TestClient) -> None:
    row = _proposal("PROP_LATE", LATE, "2024-01-01")
    _install(_base([row]))
    html = web_client.get("/proposals/prop_late").text
    assert "overdue by 2.7 years" in html
    assert "derived at read time" in html
    assert "stop maintaining this date" in html
    assert "2024-01-01" in html


def test_detail_page_without_a_date_says_nothing_about_schedule(web_client: TestClient) -> None:
    _install(_base([_proposal("PROP_UNDATED", None, None)]))
    html = web_client.get("/proposals/prop_undated").text
    assert "slip-note" not in html
    assert "derived at read time" not in html


def test_detail_page_with_a_future_date_says_nothing_about_schedule(web_client: TestClient) -> None:
    """A dated, on-schedule proposal: the date renders, the slip does not."""
    _install(_base([_proposal("PROP_OK", None, "2028-06-30")]))
    html = web_client.get("/proposals/prop_ok").text
    assert "2028-06-30" in html
    assert "slip-note" not in html
