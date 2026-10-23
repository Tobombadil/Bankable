"""Records no register lists any more are hidden from the main list and the map by default, with a
toggle (owner, 2026-10-11; `web/unlisted.py`).

Pinned here against a fake API that models `listed` (`services/api/listing.py`): a view of active
states only asks every read for `listed=true` (rows, geo, the notice's active count) and says how
many it hides in one line with a link that shows them; `unlisted=include` turns that off and is
never sent to the API; a view naming any other state hides nothing; a link's own `listed` filter is
kept; the alert link stays on the default view; the map and the list carry the toggle both ways;
the feeds and the alert keep every record. The same rules against a real store, where the counts
are the store's, are in `web/test_default_view.py`. The fake transport is duplicated, per this
repo's convention that no `web/test_*.py` imports another.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from web.alerts import saved_search_query
from web.api_client import ApiClient
from web.app import app as web_app
from web.app import list_view_href, map_view_href
from web.unlisted import hides_unlisted, include_unlisted, listed_param, show_unlisted_href
from web.viewmodels import ACTIVE_PROPOSAL_STATES

ACTIVE = ",".join(ACTIVE_PROPOSAL_STATES)
MAP_JS = (Path(__file__).resolve().parent / "static" / "js" / "map.js").read_text(encoding="utf-8")

#: `(slug, lifecycle_state, listed)`: three active records a register lists, two active ones every
#: register has dropped, a listed and a dropped built record, and a dropped withdrawn one.
STORE: tuple[tuple[str, str, bool], ...] = (
    ("live-solar", "filed", True),
    ("live-wind", "announced", True),
    ("live-storage", "contracted", True),
    ("gone-solar", "studied", False),
    ("gone-wind", "filed", False),
    ("built-live", "built", True),
    ("built-gone", "built", False),
    ("withdrawn-gone", "withdrawn", False),
)


def _matching(params: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    states = set(str(params.get("lifecycle_state") or "").split(","))
    listed = params.get("listed")
    return [r for r in STORE if r[1] in states and (not listed or r[2] == (listed == "true"))]


def _row(slug: str, state: str, listed: bool) -> dict[str, Any]:
    return {
        "public_id": f"prop_{slug.replace('-', '')}",
        "slug": slug,
        "name_canonical": slug.replace("-", " ").title(),
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 10.0,
        "iso": "ERCOT",
        "jurisdiction": "US-TX",
        "lifecycle_state": state,
        "status_raw": None,
        "listed": listed,
        "delisted_at": None if listed else "2026-10-01T00:00:00Z",
        "identifiers": {},
        "source_count": 1,
        "interconnection_point": None,
        "provenance": [
            {
                "source_id": "us.iso.ercot.gen_queue",
                "source_name": "ERCOT GIS Report",
                "source_url": "https://example.org/q",
                "retrieved_at": "2026-10-07T00:00:00Z",
                "reuse_class": "open",
                "attribution_text": "ERCOT",
                "source_record_id": slug,
                "gone_at": None if listed else "2026-10-01T00:00:00Z",
            }
        ],
    }


def proposals(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    rows = _matching(params)
    limit = int(params.get("limit") or 50)
    return 200, {
        "data": [_row(*r) for r in rows[:limit]],
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


def geo(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    rows = _matching(params)
    return 200, {
        "data": {
            "type": "FeatureCollection",
            "features": [],
            "totals": {"records": len(rows), "lifecycle_state_counts": dict(Counter(r[1] for r in rows))},
        },
        "meta": {},
    }


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
        return httpx.Response(202, json={})

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, dict(kwargs.get("params") or {})))
        return httpx.Response(404, json={"title": "not_found"})

    def close(self) -> None:
        pass

    def params_for(self, url: str) -> list[dict[str, Any]]:
        return [p for called, p in self.calls if called == url]


VOCAB = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [],
        "opportunity_status": [],
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
            "/v1/proposals": proposals,
            "/v1/proposals/geo": geo,
        }
    )
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    yield fake
    for key in ("api_client", "lag_days_default", "coverage_facts", "coverage_data", "build_info"):
        web_app.state.__dict__.pop(key, None)


def _get(path: str, params: Any = None) -> httpx.Response:
    with TestClient(web_app) as client:
        response = client.get(path, params=params)
    assert response.status_code == 200, response.text[:500]
    return response


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _result_count(html: str) -> int:
    match = re.search(r'class="result-count"[^>]*>\s*([\d,]+) proposals? match', html)
    assert match, html
    return int(match.group(1).replace(",", ""))


def _shown_active(html: str) -> int:
    match = re.search(r"Showing ([\d,]+) active proposals", _text(html))
    assert match, _text(html)
    return int(match.group(1).replace(",", ""))


def _rows(html: str) -> list[str]:
    return re.findall(r'<a class="row-link" href="/proposals/([^"]+)"', html)


def _query(href: str) -> dict[str, list[str]]:
    """The query of an href as the browser reads it: `_Page` hands over decoded attribute values;
    an href cut out of the markup by a regex is decoded first (`_raw_query`)."""
    return parse_qs(urlsplit(href).query)


def _raw_query(href: str) -> dict[str, list[str]]:
    return _query(unescape(href))


class _Page(HTMLParser):
    """Ids, the unlisted box and its label, the hidden-records line and the view-switch links."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.inputs: dict[str, dict[str, str | None]] = {}
        self.labels: dict[str, str] = {}
        self._label_for: str | None = None
        self.links: dict[str, str] = {}
        self.unlisted_links: list[tuple[str, dict[str, str | None]]] = []
        self._in_unlisted_link: dict[str, str | None] | None = None
        self._link_text = ""
        self.details: list[str] = []
        self.box_details: dict[str, str] = {}
        self.styled: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(str(a["id"]))
        if "style" in a:
            self.styled.append(tag)
        if tag == "details":
            self.details.append(str(a.get("id") or ""))
        if tag == "input" and a.get("name") == "unlisted":
            self.inputs[str(a.get("id"))] = a
            self.box_details[str(a.get("id"))] = self.details[-1] if self.details else ""
        if tag == "label":
            self._label_for = a.get("for")
            if self._label_for:
                self.labels.setdefault(self._label_for, "")
        if tag == "a":
            if a.get("id"):
                self.links[str(a["id"])] = a.get("href") or ""
            if "data-unlisted-show" in a:
                self._in_unlisted_link = a
                self._link_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._label_for = None
        if tag == "details" and self.details:
            self.details.pop()
        if tag == "a" and self._in_unlisted_link is not None:
            self.unlisted_links.append((self._link_text.strip(), self._in_unlisted_link))
            self._in_unlisted_link = None

    def handle_data(self, data: str) -> None:
        if self._label_for:
            self.labels[self._label_for] += data
        if self._in_unlisted_link is not None:
            self._link_text += data


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


# ------------------------------------------------------------------------------------ the rule
@pytest.mark.parametrize(
    ("query", "hides"),
    [
        ("", True),
        (f"lifecycle_state={ACTIVE}", True),
        ("lifecycle_state=filed", True),
        ("lifecycle_state=announced&lifecycle_state=contracted", True),
        ("unlisted=include", False),
        ("unlisted=INCLUDE", False),
        ("unlisted=yes", True),  # only `include` turns it off
        ("include_withdrawn=1", False),
        ("lifecycle_state=built", False),
        ("lifecycle_state=withdrawn", False),
        ("lifecycle_state=unknown", False),
        (f"lifecycle_state={ACTIVE},built", False),
        ("lifecycle_state=nonsense", False),
        ("listed=false", False),  # a link's own filter governs
        ("listed=true", False),  # ... and already says it
    ],
)
def test_which_views_hide_them(query: str, hides: bool) -> None:
    qp = QueryParams(query)
    assert hides_unlisted(qp) is hides
    assert listed_param(qp) == ({"listed": "true"} if hides else {})


def test_the_show_link_keeps_the_view_and_drops_the_page_cursor() -> None:
    href = show_unlisted_href(
        "/proposals", QueryParams("kind=load&cursor=abc&unlisted=no&lifecycle_state=filed")
    )
    assert _query(href) == {"kind": ["load"], "lifecycle_state": ["filed"], "unlisted": ["include"]}
    assert include_unlisted(QueryParams(urlsplit(href).query))


# ------------------------------------------------------------------------- the default view
def test_the_default_list_hides_them_and_its_counts_are_its_rows(transport: FakeTransport) -> None:
    html = _get("/proposals").text
    assert sorted(_rows(html)) == ["live-solar", "live-storage", "live-wind"]
    assert _result_count(html) == 3
    assert _shown_active(html) == 3  # the notice counts the rows under it
    calls = transport.params_for("/v1/proposals")
    rows_call = next(p for p in calls if p.get("limit") != 1)
    assert rows_call["listed"] == "true"
    assert rows_call["lifecycle_state"] == ACTIVE
    # The rows' own total is the notice's active count, so no active bucket is asked for; the other
    # buckets count states outside the view, every record of them.
    outside = [p for p in calls if p.get("limit") == 1 and p.get("listed") != "false"]
    assert sorted(p["lifecycle_state"] for p in outside) == ["built", "unknown", "withdrawn,cancelled"]
    assert all("listed" not in p for p in outside)
    # One cheap call says how many are hidden; the line says it with a way to them. In all, the
    # default list makes as many calls as before records were hidden (rows and four buckets).
    hidden = [p for p in calls if p.get("listed") == "false"]
    assert hidden == [{"lifecycle_state": ACTIVE, "listed": "false", "limit": 1, "include": "count"}]
    assert len(calls) == 5
    assert "2 proposals no register lists any more are hidden. Show them" in _text(html)
    page = _parse(html)
    assert len(page.unlisted_links) == 1
    text, link = page.unlisted_links[0]
    assert text == "Show them"
    assert _query(str(link["href"])) == {"unlisted": ["include"]}
    assert urlsplit(str(link["href"])).path == "/proposals"


def test_the_default_map_hides_them_in_its_reads_and_its_notice(transport: FakeTransport) -> None:
    html = _get("/").text
    assert _shown_active(html) == 3
    assert "2 proposals no register lists any more are hidden. Show them" in _text(html)
    _text_of_link, link = _parse(html).unlisted_links[0]
    assert urlsplit(str(link["href"])).path == "/"
    assert _query(str(link["href"])) == {"unlisted": ["include"]}
    # What map.js fetches: the geo and cluster read, and the notice it re-renders after a change.
    world = {"bbox": "-179,-85,179,85", "zoom": "1", "placement": "exact,region"}
    totals = _get("/api/proposals/geo", world).json()["data"]["totals"]
    assert totals["records"] == 3
    assert transport.params_for("/v1/proposals/geo")[-1]["listed"] == "true"
    # The map has no rows call of its own: its active bucket asks for listed records, and one more
    # call counts the hidden ones (the four buckets and that one).
    # (The opening figures' own counts, web/headline.py, are not the notice's: left out here.)
    page_calls = [
        p
        for p in transport.params_for("/v1/proposals")
        if p.get("limit") == 1 and "first_seen[from]" not in p and p.get("kind") != "load"
    ][:5]
    assert [p.get("listed") for p in page_calls] == ["true", None, None, None, "false"]
    fragment = _get(
        "/api/proposals/notice", {"placement": "exact,region", "center": "-97,31", "zoom": "5"}
    ).text
    assert _shown_active(fragment) == totals["records"]  # the count line and the notice agree
    assert "2 proposals no register lists any more are hidden" in _text(fragment)
    href = _parse(fragment).unlisted_links[0][1]["href"]
    # The link keeps the map's own place and placement, as the notice's other links do.
    assert _query(str(href)) == {
        "placement": ["exact,region"],
        "center": ["-97,31"],
        "zoom": ["5"],
        "unlisted": ["include"],
    }
    # The map's hidden count counts every placement grade, as its count line does.
    assert all("placement" not in p for p in transport.params_for("/v1/proposals"))


def test_a_view_hiding_none_says_nothing(transport: FakeTransport) -> None:
    html = _get("/proposals", {"lifecycle_state": "announced"}).text
    assert _result_count(html) == 1
    assert "no register lists any more are hidden" not in _text(html)
    assert _parse(html).unlisted_links == []


def test_an_active_subset_hides_them_too_and_says_how_many(transport: FakeTransport) -> None:
    html = _get("/proposals", {"lifecycle_state": "filed"}).text
    assert _rows(html) == ["live-solar"]
    assert "1 proposal no register lists any more is hidden. Show it" in _text(html)
    href = str(_parse(html).unlisted_links[0][1]["href"])
    assert _query(href) == {"lifecycle_state": ["filed"], "unlisted": ["include"]}
    hidden = [p for p in transport.params_for("/v1/proposals") if p.get("listed") == "false"]
    assert [p["lifecycle_state"] for p in hidden] == ["filed"]


# --------------------------------------------------------------------------------- the toggle
def test_the_toggle_shows_them_on_the_list_and_the_map(transport: FakeTransport) -> None:
    html = _get("/proposals", {"unlisted": "include"}).text
    assert sorted(_rows(html)) == ["gone-solar", "gone-wind", "live-solar", "live-storage", "live-wind"]
    assert _result_count(html) == _shown_active(html) == 5
    assert "view-notice--unlisted" not in html and _parse(html).unlisted_links == []
    assert "Clear all</a>" in html  # a choice away from the default
    map_html = _get("/", {"unlisted": "include"}).text
    assert _shown_active(map_html) == 5
    world = {"bbox": "-179,-85,179,85", "zoom": "1", "unlisted": "include"}
    assert _get("/api/proposals/geo", world).json()["data"]["totals"]["records"] == 5
    assert _shown_active(_get("/api/proposals/notice", {"unlisted": "include"}).text) == 5


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/proposals", {"unlisted": "include"}),
        ("/", {"unlisted": "include"}),
        ("/api/proposals/geo", {"unlisted": "include", "bbox": "-179,-85,179,85", "zoom": "1"}),
        ("/api/proposals/notice", {"unlisted": "include"}),
        ("/proposals", {"unlisted": "anything"}),
        ("/proposals", {}),
        ("/", {}),
    ],
)
def test_the_toggle_is_never_sent_to_the_api(
    transport: FakeTransport, path: str, params: dict[str, str]
) -> None:
    _get(path, params)
    proposal_calls = [(url, p) for url, p in transport.calls if url.startswith("/v1/proposals")]
    assert proposal_calls
    assert all("unlisted" not in p for _url, p in transport.calls)
    if params.get("unlisted") == "include":
        assert all("listed" not in p for _url, p in proposal_calls)


@pytest.mark.parametrize(
    ("params", "expected_total"),
    [
        ({"lifecycle_state": "built"}, 2),
        ({"lifecycle_state": "withdrawn"}, 1),
        ({"lifecycle_state": f"{ACTIVE},built"}, 7),
        ({"include_withdrawn": "1"}, 6),
        ([("lifecycle_state", "announced"), ("lifecycle_state", "built")], 3),
    ],
)
def test_a_view_naming_any_other_state_hides_nothing(
    transport: FakeTransport, params: Any, expected_total: int
) -> None:
    html = _get("/proposals", params).text
    assert _result_count(html) == expected_total
    assert "are hidden. Show" not in _text(html) and "is hidden. Show" not in _text(html)
    assert all("listed" not in p for url, p in transport.calls if url.startswith("/v1/proposals"))
    transport.calls.clear()
    geo_params = [*(params.items() if isinstance(params, dict) else params), ("bbox", "-179,-85,179,85")]
    assert _get("/api/proposals/geo", geo_params).json()["data"]["totals"]["records"] == expected_total
    assert "listed" not in transport.params_for("/v1/proposals/geo")[0]


def test_a_links_own_listed_filter_is_kept_and_not_counted_twice(transport: FakeTransport) -> None:
    """A company page's "No longer listed" row links `lifecycle_state=<active>&listed=false`."""
    html = _get("/proposals", {"lifecycle_state": ACTIVE, "listed": "false"}).text
    assert sorted(_rows(html)) == ["gone-solar", "gone-wind"]
    rows_call = next(p for p in transport.params_for("/v1/proposals") if p.get("limit") != 1)
    assert rows_call["listed"] == "false"
    assert "no register lists any more are hidden" not in _text(html)
    # No hidden-records count: the rows (whose total is the active bucket) and the three other
    # notice buckets, each with the link's own filter.
    calls = transport.params_for("/v1/proposals")
    assert len(calls) == 4
    assert all(p["listed"] == "false" for p in calls)


# ------------------------------------------------------------------- alerts, feeds and exports
@pytest.mark.parametrize("path", ["/proposals", "/"])
@pytest.mark.parametrize("params", [{}, {"unlisted": "include"}])
def test_the_alert_link_stays_and_watches_every_record(
    transport: FakeTransport, path: str, params: dict[str, str]
) -> None:
    html = _get(path, params).text
    match = re.search(r'href="(/alerts/new\?[^"]+)"', html)
    assert match, "the default view offers 'Save this search as an alert'"
    alert_params = QueryParams(urlsplit(unescape(match.group(1))).query)
    assert "listed" not in alert_params
    query = saved_search_query("proposal", alert_params, origin=alert_params.get("origin"))
    assert "listed" not in query and "unlisted" not in query
    assert query["lifecycle_state"] == ACTIVE


@pytest.mark.parametrize("path", ["/proposals", "/"])
def test_the_feed_twins_keep_every_record(transport: FakeTransport, path: str) -> None:
    """The RSS and JSON feeds are change feeds: a project leaving its register is news there."""
    html = _get(path, {"unlisted": "include", "kind": "generation"}).text
    feeds = re.findall(r'href="(/feeds/proposals\.[a-z]+\?[^"]*)"', html)
    assert feeds
    for href in feeds:
        query = _raw_query(href)
        assert "listed" not in query and "unlisted" not in query
        assert query["kind"] == ["generation"]
    default = _get(path).text
    for href in re.findall(r'href="(/feeds/proposals\.[a-z]+\?[^"]*)"', default):
        assert "listed" not in _raw_query(href)


def test_the_list_offers_no_export_that_could_disagree_with_it(transport: FakeTransport) -> None:
    """The list links no CSV or JSON export (exports are `POST /v1/exports`, a signed-in API call
    the site does not offer); its only machine-readable twins are the feeds above. A future export
    link must be built from the list's own API query (`web/app.py::_proposal_params`)."""
    html = _get("/proposals").text
    assert "/v1/exports" not in html
    assert not re.search(r'href="[^"]*(\.csv|format=csv|text/csv)', html)


# ------------------------------------------------------------------ the map and the list links
def test_the_view_switch_links_carry_the_toggle_both_ways(transport: FakeTransport) -> None:
    qp = QueryParams("unlisted=include&kind=generation")
    assert _query(list_view_href(qp)) == {"kind": ["generation"], "unlisted": ["include"]}
    assert _query(map_view_href(qp)) == {"kind": ["generation"], "unlisted": ["include"]}
    assert "unlisted" not in list_view_href(QueryParams("kind=generation"))
    list_page = _parse(_get("/proposals", {"unlisted": "include", "kind": "generation"}).text)
    assert _query(list_page.links["view-as-map"]) == {"kind": ["generation"], "unlisted": ["include"]}
    # The map's "Open these in the list" (owner, 2026-10-22: it replaced the map's view switch).
    map_page = _parse(_get("/", {"unlisted": "include"}).text)
    assert _query(map_page.links["map-list-link"]) == {"unlisted": ["include"]}


def test_map_js_keeps_the_toggle_in_the_url_and_the_list_link_but_not_the_feed() -> None:
    assert 'params.get("unlisted")' in MAP_JS
    assert 'if (filters.unlisted) params.set("unlisted", "include");' in MAP_JS
    # appendProposalFilters feeds the URL, the list link, the geo read and the notice fragment.
    append = MAP_JS.split("function appendProposalFilters", 1)[1].split("\n  }\n", 1)[0]
    assert "filters.unlisted" in append
    # The map builds no feed link of its own any more: the alert page offers the feed (owner,
    # 2026-10-22), through web/feeds.py `feed_href`, which leaves the toggle out.
    assert "view-as-feed" not in MAP_JS
    assert "unlisted: unlistedBox ? unlistedBox.checked : false" in MAP_JS
    assert 'unlistedBox.addEventListener("change", applyFilters)' in MAP_JS
    clear = MAP_JS.split('getElementById("mf-clear")', 1)[1].split("applyFilters();", 1)[0]
    assert "unlistedBox.checked = false" in clear
    assert "a[data-unlisted-show]" in MAP_JS


# ------------------------------------------------------------------------------ the markup
@pytest.mark.parametrize(("path", "prefix"), [("/proposals", "f"), ("/", "mf")])
def test_the_box_is_labelled_grouped_with_status_and_follows_the_url(
    transport: FakeTransport, path: str, prefix: str
) -> None:
    html = _get(path).text
    page = _parse(html)
    box_id = f"{prefix}-unlisted"
    box = page.inputs[box_id]
    assert box["type"] == "checkbox" and box["value"] == "include"
    assert "checked" not in box and "disabled" not in box
    # site.js counts it as a filter set only when it is ticked (no `data-default`).
    assert "data-default" not in box
    assert " ".join(page.labels[box_id].split()) == "Include proposals no register lists any more"
    assert page.box_details[box_id] == f"{prefix}-status"
    assert len(page.ids) == len(set(page.ids)), "ids are unique"
    assert page.styled == [], "no style attributes (the CSP forbids them)"
    assert "Always included while Built, Withdrawn or Status unknown is ticked." in _text(html)
    assert re.search(
        rf'id="{prefix}-status"[^>]*>\s*<summary>Status:&nbsp;<span data-status-summary>active</span>', html
    )
    on = _get(path, {"unlisted": "include"}).text
    assert "checked" in _parse(on).inputs[box_id]
    assert re.search(r"<span data-status-summary>active, no longer listed</span>", on)


def test_the_hidden_line_is_a_status_with_a_named_link(transport: FakeTransport) -> None:
    html = _get("/proposals").text
    line = re.search(r'<p class="view-notice view-notice--unlisted" role="status">(.*?)</p>', html, re.S)
    assert line, html
    assert re.search(
        r'<a href="[^"]+" class="view-notice__unlisted" data-unlisted-show>Show them</a>', line.group(1)
    )
    # Not the built link's class, which map.js turns into ticking Built.
    assert "view-notice__show" not in line.group(1)


def test_the_box_note_shows_only_while_a_status_outside_the_default_is_ticked() -> None:
    css = (Path(__file__).resolve().parent / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".filter-unlisted__note { display: none; margin: 0; }" in css
    assert (
        '.filter-status:has(input[name="lifecycle_state"]:not([data-default]):checked) '
        ".filter-unlisted__note { display: block; }"
    ) in css
