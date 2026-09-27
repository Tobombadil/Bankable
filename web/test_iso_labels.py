"""ISO tokens read as the operator's name on the web (lane E15): the store, the API, `?iso=` and
alerts keep the spec's `Iso` token (`ISONE`, docs/00-PLAN.md 2026-09-27 lane E14); a page shows
"ISO-NE". Every other token renders as itself. `web/viewmodels.py::iso_label`.

The page half drives `web/app.py` against a fake `Transport`, duplicated from
`web/test_slippage_view.py` because no `web/test_*.py` imports another (this repo's convention).
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import ISO_DISPLAY_LABELS, flatten_proposal, iso_label

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
        "slip_bucket": [],
    }
}


@pytest.mark.parametrize(
    ("token", "label"),
    [
        ("ISONE", "ISO-NE"),
        ("ERCOT", "ERCOT"),
        ("CAISO", "CAISO"),
        ("PJM", "PJM"),
        ("TVA", "TVA"),
        (None, None),
        ("", ""),
    ],
)
def test_iso_label(token: str | None, label: str | None) -> None:
    assert iso_label(token) == label


def test_only_isone_is_relabelled() -> None:
    assert ISO_DISPLAY_LABELS == {"ISONE": "ISO-NE"}


def test_flatten_proposal_keeps_the_token_and_adds_the_label() -> None:
    record = flatten_proposal({"public_id": "prop_1", "slug": "p", "name_canonical": "P", "iso": "ISONE"})
    assert record["iso"] == "ISONE"
    assert record["iso_label"] == "ISO-NE"


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


def _proposal(public_id: str, iso: str | None) -> dict[str, Any]:
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
        "iso": iso,
        "proposed_online_date": None,
        "schedule_slip": None,
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


def test_detail_page_shows_iso_ne_not_the_token(web_client: TestClient) -> None:
    _install(_base([_proposal("PROP_NE", "ISONE")]))
    html = web_client.get("/proposals/prop_ne").text
    assert "<dd>ISO-NE</dd>" in html
    assert ">ISONE<" not in html


def test_detail_page_leaves_other_tokens_alone(web_client: TestClient) -> None:
    _install(_base([_proposal("PROP_TX", "ERCOT")]))
    html = web_client.get("/proposals/prop_tx").text
    assert "<dd>ERCOT</dd>" in html


def test_admin_record_page_labels_the_row_and_keeps_the_token_in_the_form() -> None:
    """The admin read-only row shows the label; the edit form submits the stored token, so saving a
    record never rewrites `ISONE` to `ISO-NE`. The web has no `?iso=` filter today, so the only
    query-string use of the token is the API's own (`GET /v1/proposals?iso=ISONE`)."""
    from web.admin.records import templates

    assert templates.env.from_string("{{ iso_label('ISONE') }}").render() == "ISO-NE"
    source = pathlib.Path(web_app_root(), "templates/admin/records/proposal_detail.html").read_text()
    assert "iso_label(proposal.iso)" in source
    assert 'name="iso" value="{{ proposal.iso or \'\' }}"' in source


def web_app_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent
