"""The Status control on the map and the list (audit 2026-10-07 UX-1, docs/30 §4.1, docs/31 §5.9).

Before it, nothing on either page could show a built proposal: the map bar had technology, kind,
jurisdiction and a withdrawn box, and the list bar the same plus capacity and schedule. The default
view (active states only) hid 1,844 built proposals, 484 of them operating data centres, and the
notice that counted them offered no way to them; only a hand-typed `?lifecycle_state=built` did.

These tests pin: one control (same groups, same names) on both bars; the default is still the
active set; a list form that sends one `lifecycle_state` per checked group asks the API for their
union; leaving the boxes alone is still the default view (the notice stays); the notice's built
count links to the view with Built added; and the map/list switch carries the choice. The fake
transport is duplicated, per this repo's convention that no `web/test_*.py` imports another.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from web.api_client import ApiClient
from web.app import app as web_app
from web.app import list_view_href, map_view_href
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    PROPOSAL_STATUS_GROUPS,
    WITHDRAWN_PROPOSAL_STATES,
    lifecycle_query_items,
    proposal_status_choices,
    resolve_proposal_lifecycle_param,
    with_built_href,
)

ACTIVE = ",".join(ACTIVE_PROPOSAL_STATES)
STATE_COUNTS: dict[str, int] = {"announced": 40, "filed": 6, "withdrawn": 2, "built": 1844, "unknown": 5}


class FakeTransport:
    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        answer = self.responses.get(url)
        if answer is None:
            return httpx.Response(404, json={"title": "not_found", "detail": url})
        status, body = answer(params or {}) if callable(answer) else answer
        return httpx.Response(status, json=body)

    def post(self, url: str, *, json: Any = None, cookies: Any = None) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def close(self) -> None:
        pass

    def params_for(self, url: str) -> list[dict[str, Any]]:
        return [p for called, p in self.calls if called == url]


def counted_list(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    states = str(params.get("lifecycle_state") or "").split(",")
    total = sum(STATE_COUNTS.get(s, 0) for s in states)
    return 200, {
        "data": [],
        "meta": {"total": total, "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


VOCAB = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "load"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}


@pytest.fixture()
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
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


class _StatusBoxes(HTMLParser):
    """The `lifecycle_state` checkboxes inside one form, and the notice's links."""

    def __init__(self, form_id: str) -> None:
        super().__init__()
        self.form_id = form_id
        self._in_form = False
        self.boxes: list[dict[str, str | None]] = []
        self.summary = ""
        self._in_summary = False
        self.notice_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form" and a.get("id") == self.form_id:
            self._in_form = True
        if self._in_form and tag == "input" and a.get("name") == "lifecycle_state":
            self.boxes.append(a)
        if tag == "span" and "data-status-summary" in a:
            self._in_summary = True
        if tag == "a" and "view-notice__show" in (a.get("class") or ""):
            self.notice_links.append(a.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False
        if tag == "span":
            self._in_summary = False

    def handle_data(self, data: str) -> None:
        if self._in_summary:
            self.summary += data


def _parse(html: str, form_id: str) -> _StatusBoxes:
    parser = _StatusBoxes(form_id)
    parser.feed(html)
    return parser


# ---------------------------------------------------------------------- the view-model rules
def test_the_groups_cover_every_state_once_and_the_first_three_are_the_default() -> None:
    states = [s for _key, _label, group in PROPOSAL_STATUS_GROUPS for s in group]
    assert len(states) == len(set(states))
    assert set(states) == set(ACTIVE_PROPOSAL_STATES) | set(WITHDRAWN_PROPOSAL_STATES) | {"built", "unknown"}
    assert {s for _k, _l, g in PROPOSAL_STATUS_GROUPS[:3] for s in g} == set(ACTIVE_PROPOSAL_STATES)


def test_repeated_lifecycle_state_values_are_their_union() -> None:
    qp = QueryParams(
        [("lifecycle_state", "announced"), ("lifecycle_state", "built"), ("lifecycle_state", "built")]
    )
    assert resolve_proposal_lifecycle_param(qp) == ("announced,built", True, False)


def test_the_default_set_sent_by_the_boxes_is_still_the_default_view() -> None:
    groups = [",".join(g) for _k, _l, g in PROPOSAL_STATUS_GROUPS[:3]]
    qp = QueryParams([("lifecycle_state", v) for v in groups])
    assert resolve_proposal_lifecycle_param(qp) == (ACTIVE, False, False)
    with_withdrawn = QueryParams([("lifecycle_state", v) for v in [*groups, "withdrawn,cancelled"]])
    csv, explicit, include = resolve_proposal_lifecycle_param(with_withdrawn)
    assert (explicit, include) == (False, True)
    assert set(csv.split(",")) == set(ACTIVE_PROPOSAL_STATES) | set(WITHDRAWN_PROPOSAL_STATES)
    # A plain mapping (alerts pass one) still works.
    assert resolve_proposal_lifecycle_param({"lifecycle_state": "built"}) == ("built", True, False)


def test_status_choices_default_partial_and_summary() -> None:
    default = proposal_status_choices(QueryParams(""))
    assert [b["checked"] for b in default["boxes"]] == [True, True, True, False, False, False]
    assert default["summary"] == "active"
    built = proposal_status_choices(QueryParams(f"lifecycle_state={ACTIVE},built"))
    assert [b["checked"] for b in built["boxes"]] == [True, True, True, True, False, False]
    assert built["summary"] == "active, built"
    # A hand-written single state inside a group keeps exactly that state on resubmission.
    partial = proposal_status_choices(QueryParams("lifecycle_state=filed"))
    in_process = next(b for b in partial["boxes"] if b["key"] == "in_process")
    assert in_process["checked"] and in_process["value"] == "filed"
    assert in_process["note"] == "only filed"
    assert partial["summary"] == "in process"


def test_query_items_and_view_switch_links_carry_the_choice() -> None:
    assert lifecycle_query_items(QueryParams("")) == []
    assert lifecycle_query_items(QueryParams("include_withdrawn=1")) == [("include_withdrawn", "1")]
    multi = QueryParams([("kind", "load"), ("lifecycle_state", "announced"), ("lifecycle_state", "built")])
    assert lifecycle_query_items(multi) == [("lifecycle_state", "announced,built")]
    expected = {"kind": ["load"], "lifecycle_state": ["announced,built"]}
    assert parse_qs(urlsplit(map_view_href(multi)).query) == expected
    assert parse_qs(urlsplit(list_view_href(multi)).query) == expected


def test_built_href_adds_built_to_the_current_view() -> None:
    href = with_built_href("/proposals", QueryParams("kind=load&include_withdrawn=1&cursor=abc"))
    query = parse_qs(urlsplit(href).query)
    assert query["kind"] == ["load"]
    assert "cursor" not in query and "include_withdrawn" not in query
    assert set(query["lifecycle_state"][0].split(",")) == (
        set(ACTIVE_PROPOSAL_STATES) | set(WITHDRAWN_PROPOSAL_STATES) | {"built"}
    )


# ---------------------------------------------------------------------- the pages
@pytest.mark.parametrize(
    ("path", "form_id"), [("/?kind=load", "map-filters"), ("/proposals?kind=load", "filters")]
)
def test_both_bars_offer_the_same_status_boxes_defaulting_to_active(
    transport: FakeTransport, path: str, form_id: str
) -> None:
    with TestClient(web_app) as client:
        response = client.get(path)
    assert response.status_code == 200
    page = _parse(response.text, form_id)
    assert [b["data-status-group"] for b in page.boxes] == [k for k, _l, _g in PROPOSAL_STATUS_GROUPS]
    assert [b["value"] for b in page.boxes] == [",".join(g) for _k, _l, g in PROPOSAL_STATUS_GROUPS]
    assert ["checked" in b for b in page.boxes] == [True, True, True, False, False, False]
    # The 400 px "Filters (N active)" count treats the default boxes as unset (site.js).
    assert ["data-default" in b for b in page.boxes] == [True, True, True, False, False, False]
    assert page.summary == "active"
    # The notice says how many are built and links to them, with this page's filters kept.
    assert "1,844 built" in response.text
    assert len(page.notice_links) == 1
    linked = parse_qs(urlsplit(page.notice_links[0].replace("&amp;", "&")).query)
    assert linked["kind"] == ["load"]
    assert "built" in linked["lifecycle_state"][0].split(",")


def test_a_list_form_with_built_checked_asks_the_api_for_active_and_built(transport: FakeTransport) -> None:
    groups = [",".join(g) for _k, _l, g in PROPOSAL_STATUS_GROUPS]
    with TestClient(web_app) as client:
        response = client.get(
            "/proposals", params=[("kind", "load"), *[("lifecycle_state", g) for g in groups[:4]]]
        )
    assert response.status_code == 200
    list_call = transport.params_for("/v1/proposals")[0]
    assert set(list_call["lifecycle_state"].split(",")) == set(ACTIVE_PROPOSAL_STATES) | {"built"}
    page = _parse(response.text, "filters")
    assert ["checked" in b for b in page.boxes] == [True, True, True, True, False, False]
    # The view-as-map link carries the same choice as one csv.
    csv = "announced%2Cfiled%2Cstudied%2Cpermitted%2Cunder_construction%2Ccontracted%2Cbuilt"
    assert f"lifecycle_state={csv}" in response.text.replace("&amp;", "&")


def test_untouched_boxes_keep_the_default_view_and_its_notice(transport: FakeTransport) -> None:
    groups = [",".join(g) for _k, _l, g in PROPOSAL_STATUS_GROUPS[:3]]
    with TestClient(web_app) as client:
        response = client.get("/proposals", params=[("lifecycle_state", g) for g in groups])
    assert response.status_code == 200
    assert "Showing 46 active proposals" in response.text
    assert "Clear all</a>" not in response.text
