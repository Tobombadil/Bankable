"""The home map passes every proposal list filter through, as the list pages do since 2026-09-27
(`web/test_list_filter_passthrough.py`). Measured 2026-09-29: `/?kind=load` drew 5,853 proposals
under a server notice counting 46, because `web/static/js/map.js` knew four filter names by hand,
and then rewrote the URL without `kind`.

The page now hands map.js the passthrough names (`data-passthrough` on `#map-filters`, rendered from
`PROPOSAL_PASSTHROUGH_FILTERS`), so there is one list; these tests pin that attribute to the tuple,
show a linked filter reaching the page and its breakdown, and cover the notice fragment map.js
fetches after a filter change. The browser half (the geo request carries the filter, the count
agrees with the API, the URL keeps it) is `web/test_e2e.py::test_map_keeps_and_forwards_a_linked_kind_filter`.
The fake transport is duplicated from `web/test_list_filter_passthrough.py`, per this repo's
convention that no `web/test_*.py` imports another.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import PROPOSAL_PASSTHROUGH_FILTERS
from web.app import app as web_app
from web.viewmodels import ALL_PROPOSAL_LIFECYCLE_STATES, PROPOSAL_KIND_LABELS


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _respond(self, url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        if url in self.responses:
            answer = self.responses[url]
            status, body = answer(params or {}) if callable(answer) else answer
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url, params)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, dict(json or {})))
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
        self.calls.append((method, url, dict(json if json is not None else params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass

    def params_for(self, url: str) -> list[dict[str, Any]]:
        return [params for _method, called, params in self.calls if called == url]


HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
PROPOSAL_KINDS = ("generation", "storage", "load", "transmission")
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "storage"}, {"value": "solar"}],
        "proposal_kind": [{"value": k} for k in PROPOSAL_KINDS],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}
# 46 active (the measured `kind=load` answer), 2 withdrawn, 3 built.
STATE_COUNTS: dict[str, int] = {"announced": 40, "filed": 6, "withdrawn": 2, "built": 3}


def counted_list(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """`GET /v1/proposals?limit=1&include=count&lifecycle_state=...`: the lifecycle notice's counts
    (frontend audit F9: they used to come from a world-wide `/v1/proposals/geo` at zoom 1)."""
    states = str(params.get("lifecycle_state") or "").split(",")
    total = sum(STATE_COUNTS.get(state, 0) for state in states)
    return 200, {
        "data": [],
        "meta": {"total": total, "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
    }


@pytest.fixture()
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/proposals": counted_list,
        }
    )
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    yield fake
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


class _FilterForm(HTMLParser):
    """Collects `#map-filters`'s attributes, the Kind select's options, the Kind label's target
    and the `#map-notice` text: the parts of the page map.js reads."""

    def __init__(self) -> None:
        super().__init__()
        self.form_attrs: dict[str, str | None] | None = None
        self.kind_options: list[tuple[str, str, bool]] = []
        self.kind_label_for: str | None = None
        self.notice_text = ""
        self._in_kind_select = False
        self._option: tuple[str, bool] | None = None
        self._option_text = ""
        self._label_for: str | None = None
        self._label_text = ""
        self._notice_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if self._notice_depth:
            self._notice_depth += 1
        if tag == "div" and a.get("id") == "map-notice":
            self._notice_depth = 1
        if tag == "form" and a.get("id") == "map-filters":
            self.form_attrs = a
        if tag == "select" and a.get("id") == "mf-kind":
            self._in_kind_select = True
        if tag == "option" and self._in_kind_select:
            self._option = (a.get("value") or "", "selected" in a)
            self._option_text = ""
        if tag == "label":
            self._label_for = a.get("for")
            self._label_text = ""

    def handle_endtag(self, tag: str) -> None:
        if self._notice_depth:
            self._notice_depth -= 1
        if tag == "option" and self._option is not None:
            value, selected = self._option
            self.kind_options.append((value, self._option_text.strip(), selected))
            self._option = None
        if tag == "select":
            self._in_kind_select = False
        if tag == "label" and self._label_text.strip() == "Kind":
            self.kind_label_for = self._label_for

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option_text += data
        if self._label_for is not None:
            self._label_text += data
        if self._notice_depth:
            self.notice_text += data


def _parse(html: str) -> _FilterForm:
    parser = _FilterForm()
    parser.feed(html)
    return parser


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_the_page_hands_map_js_exactly_the_passthrough_filters(transport: FakeTransport) -> None:
    """No second list in JS: the names map.js keeps and forwards are the tuple the list page uses."""
    with TestClient(web_app) as client:
        page = _parse(client.get("/").text)
    assert page.form_attrs is not None, "#map-filters missing"
    attribute = page.form_attrs.get("data-passthrough") or ""
    assert tuple(attribute.split(",")) == PROPOSAL_PASSTHROUGH_FILTERS
    # The names are joined with commas, so none may contain one.
    assert all("," not in name for name in PROPOSAL_PASSTHROUGH_FILTERS)


def test_a_linked_kind_and_state_reach_the_map_page(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        response = client.get("/?kind=load&state=US-VA&utm_source=newsletter")
    assert response.status_code == 200
    page = _parse(response.text)
    assert page.form_attrs is not None
    names = (page.form_attrs.get("data-passthrough") or "").split(",")
    # Both linked filters are names map.js reads, keeps and forwards; the tracking parameter is not.
    assert "kind" in names
    assert "state" in names
    assert "utm_source" not in names
    # The server-rendered breakdown counted the same filtered set the map will draw.
    breakdown = transport.params_for("/v1/proposals")
    assert len(breakdown) == 4  # active, withdrawn, built, unknown: one `include=count` call each (UX-1)
    assert all(p.get("kind") == "load" and p.get("state") == "US-VA" for p in breakdown)
    assert all(p.get("limit") == 1 and p.get("include") == "count" for p in breakdown)
    assert all("utm_source" not in p for p in breakdown)
    # Frontend audit F9: no world-wide clustered geo query is run just to count states.
    assert transport.params_for("/v1/proposals/geo") == []
    assert "Showing 46 active proposals" in _squash(page.notice_text)
    # The Kind select shows the linked value, so the control and the URL agree before any JS runs.
    assert [value for value, _label, selected in page.kind_options if selected] == ["load"]


def test_kind_select_is_labelled_and_names_every_vocabulary_kind(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        page = _parse(client.get("/").text)
    assert page.kind_label_for == "mf-kind"
    assert page.kind_options[0] == ("", "All", False)
    offered = {value: label for value, label, _selected in page.kind_options[1:]}
    assert tuple(offered) == PROPOSAL_KINDS
    assert offered["load"] == "Load (data centres, large loads)"
    assert all(offered[k] == PROPOSAL_KIND_LABELS[k] for k in PROPOSAL_KINDS)


def test_an_unlabelled_kind_token_renders_as_words_not_as_itself(transport: FakeTransport) -> None:
    transport.responses["/v1/meta/vocabularies"] = (
        200,
        {"data": {**VOCAB["data"], "proposal_kind": [{"value": "geothermal_heat"}]}},
    )
    with TestClient(web_app) as client:
        page = _parse(client.get("/").text)
    # The value stays the token; the visible text is never the raw token (web/labels.py `humanise`).
    assert ("geothermal_heat", "Geothermal heat", False) in page.kind_options


def test_notice_fragment_counts_the_filters_map_js_applied(transport: FakeTransport) -> None:
    """What map.js swaps into `#map-notice` after a filter change: the same breakdown, the same
    wording, for the filters it sends -- and nothing it was not asked to count."""
    with TestClient(web_app) as client:
        page_notice = _parse(client.get("/?kind=load&state=US-VA").text).notice_text
        transport.calls.clear()
        fragment = client.get(
            "/api/proposals/notice",
            params={"kind": "load", "state": "US-VA", "placement": "exact,region", "utm_source": "x"},
        )
    assert fragment.status_code == 200
    assert fragment.headers["content-type"].startswith("text/html")
    counts = transport.params_for("/v1/proposals")
    assert len(counts) == 4  # active, withdrawn, built, unknown (UX-1 splits "other")
    assert transport.params_for("/v1/proposals/geo") == []
    for params in counts:
        assert params["kind"] == "load"
        assert params["state"] == "US-VA"
        # The map's count line counts every placement grade, so its notice does too.
        assert "placement" not in params
        assert "utm_source" not in params
    # Every lifecycle state is counted exactly once across the four buckets.
    states = [s for params in counts for s in params["lifecycle_state"].split(",")]
    assert len(states) == len(set(states))
    assert set(states) == set(ALL_PROPOSAL_LIFECYCLE_STATES)
    fragment_text = _parse(f'<div id="map-notice">{fragment.text}</div>').notice_text
    assert _squash(fragment_text) == _squash(page_notice)
    assert "Showing 46 active proposals" in _squash(fragment_text)
    assert 'role="status"' in fragment.text


def test_notice_fragment_is_empty_when_a_link_names_its_lifecycle_state(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        fragment = client.get("/api/proposals/notice?lifecycle_state=built")
    assert fragment.status_code == 200
    assert fragment.text.strip() == ""


def test_include_withdrawn_notice_counts_what_the_map_draws(transport: FakeTransport) -> None:
    """With withdrawn/cancelled included the map asks for active + withdrawn states, not built or
    unknown; the notice used to call its total "every lifecycle state" and count those too
    (measured 2026-09-29: notice 10,584, map 9,068)."""
    with TestClient(web_app) as client:
        text = _squash(_parse(client.get("/?include_withdrawn=1").text).notice_text)
    assert "Showing 48 proposals, including 2 withdrawn or cancelled" in text
    # UX-1: built and unknown-state are counted apart (the fake store has 3 built, 0 unknown).
    assert "(3 built proposals are outside this view)." in text
    assert "every lifecycle state" not in text
