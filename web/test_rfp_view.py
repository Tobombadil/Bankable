"""RFPs & funding never looks empty (lane rfp-view, 2026-10-23).

On 2026-10-23 the store held 16 utility RFPs (14 closed, 1 announced, 1 unknown) and the page opened
on `status=open`, so it showed none. Held here, against a hand-written fake of the API (no database):

- the page's default status is `open,announced` (the API's own default stays `open`); an announced
  row says "Not open yet" beside its chip, with the opening date it states when that is ahead;
- while the reader has not chosen a status, a "Recently closed (last 12 months)" section follows
  the list: closed notices whose deadline fell in the last 365 days, newest first, at most ten, with
  the count, a link to the full closed list and its own credit line. It never mixes into the list,
  never shows a row the API answers with another status, and is left out on a chosen status, on a
  later page and when the API cannot answer;
- with nothing open, announced or recently closed, a plain empty state says why and links to the
  closed list, the issuers read and an alert;
- the Status select, the standfirst, the canonical URL, the feed and the alert follow the default.

The fake is duplicated rather than imported, per this repo's convention that no `web/test_*.py`
imports another.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator, Mapping
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import (
    OPPORTUNITY_PAGE_DEFAULT_STATUS,
    announced_opens_on,
    opportunity_status_chosen,
    opportunity_status_form_value,
    opportunity_status_param,
    opportunity_status_phrase,
    recently_closed_window,
)

PROV = {
    "source_id": "us.rfp.ct_deep",
    "source_name": "CT DEEP news releases",
    "source_url": "https://portal.ct.gov/deep/news-releases/x",
    "retrieved_at": "2026-10-23T06:00:00Z",
    "reuse_class": "attribution",
    "attribution_text": "Source: Connecticut Department of Energy and Environmental Protection.",
    "is_primary": True,
}
AEP_PROV = {
    "source_id": "us.rfp.pso",
    "source_name": "PSO Energy Provider RFPs",
    "source_url": "https://www.psoklahoma.com/business/b2b/energy-rfps/",
    "retrieved_at": "2026-10-23T06:00:00Z",
    "reuse_class": "noncommercial",
    "attribution_text": "Source: Public Service Company of Oklahoma.",
    "is_primary": True,
}


def _notice(slug: str, status: str, due_at: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "public_id": f"opp_{slug}",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "kind": "rfp",
        "status": status,
        "due_at": due_at,
        "jurisdiction": "US-CT",
        "technologies": ["wind_offshore"],
        "provenance": [PROV],
        **extra,
    }


ANNOUNCED = _notice("zero-carbon-draft-rfp", "announced", None, open_at="2099-01-15")
OPEN = _notice("all-source-rfp", "open", "2099-03-16T00:00:00Z")
CLOSED = [
    _notice(
        f"closed-rfp-{i}", "closed", f"2026-{9 - i // 3:02d}-{28 - i:02d}T00:00:00Z", provenance=[AEP_PROV]
    )
    for i in range(12)
]
VOCAB = {
    "data": {
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": s} for s in ("unknown", "announced", "open", "closed")],
        "opportunity_technology": [{"value": "wind_offshore"}],
    }
}


def _is_section_call(params: Mapping[str, Any]) -> bool:
    """The "Recently closed" call: closed notices, a page of ten (the main list asks for no limit,
    the probes for one)."""
    return params.get("status") == "closed" and params.get("limit") == 10


def _envelope(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows) if total is None else total, "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": {"sources": []},
    }


class Store:
    """What the fake API holds: the main list's rows by status, and the closed notices of the last
    year (the section's call: `status=closed` with a page of ten)."""

    def __init__(
        self, rows: list[dict[str, Any]], closed: list[dict[str, Any]], closed_total: int | None = None
    ):
        self.rows = rows
        self.closed = closed
        self.closed_total = closed_total
        self.closed_answer: tuple[int, Any] | None = None

    def __call__(self, params: Mapping[str, Any]) -> tuple[int, Any]:
        statuses = set(str(params.get("status") or "open").split(","))
        if _is_section_call(params):
            if self.closed_answer is not None:
                return self.closed_answer
            limit = int(params.get("limit") or 50)
            return 200, _envelope(self.closed[:limit], total=self.closed_total or len(self.closed))
        rows = [r for r in self.rows if r["status"] in statuses]
        if params.get("limit") == 1:
            return 200, _envelope(rows[:1], total=len(rows))
        return 200, _envelope(rows)


class FakeTransport:
    """Canned responses keyed by path (a callable answers from the params); every call recorded."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, **_: Any) -> httpx.Response:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        self.calls.append((url, clean))
        answer = self.responses.get(url)
        if answer is None:
            return httpx.Response(404, json={"title": "not_found", "detail": url})
        status, body = answer(clean) if callable(answer) else answer
        return httpx.Response(status, json=body)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def request(
        self, method: str, url: str, *, params: Mapping[str, Any] | None = None, **_: Any
    ) -> httpx.Response:
        return self.get(url, params=params)

    def close(self) -> None:
        pass

    def params(self, url: str) -> list[dict[str, Any]]:
        return [p for called, p in self.calls if called == url]


def _install(store: Store) -> FakeTransport:
    transport = FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/opportunities": store,
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _reset() -> None:
    for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
        web_app.state.__dict__.pop(key, None)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    _reset()
    with TestClient(web_app) as c:
        yield c
    _reset()


def _main_call(fake: FakeTransport) -> dict[str, Any]:
    calls = [p for p in fake.params("/v1/opportunities") if p.get("limit") != 1 and not _is_section_call(p)]
    assert calls, fake.calls
    return calls[0]


def _closed_calls(fake: FakeTransport) -> list[dict[str, Any]]:
    return [p for p in fake.params("/v1/opportunities") if _is_section_call(p)]


def _section(html: str) -> str:
    match = re.search(r'<section class="recent-closed".*?</section>', html, re.S)
    assert match, "no recently closed section"
    return match.group(0)


def _table(html: str) -> str:
    match = re.search(r'<table class="record-table record-table--compact">.*?</table>', html, re.S)
    assert match, "no table"
    return match.group(0)


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


# ------------------------------------------------------------------------------- view models
def test_the_page_default_is_open_and_announced_and_a_named_default_is_no_choice() -> None:
    assert OPPORTUNITY_PAGE_DEFAULT_STATUS == "open,announced"
    assert opportunity_status_param({}) == "open,announced"
    assert opportunity_status_param({"status": ""}) == "open,announced"
    assert opportunity_status_param({"status": "closed"}) == "closed"
    assert opportunity_status_param({"status": "all"}).split(",") == [
        "unknown",
        "announced",
        "open",
        "frozen",
        "reinstated",
        "closed",
        "cancelled",
        "awarded",
    ]
    for unchosen in ({}, {"status": ""}, {"status": "open,announced"}, {"status": "announced, open"}):
        assert not opportunity_status_chosen(unchosen), unchosen
        assert opportunity_status_form_value(unchosen) == "open,announced"
    for chosen in ({"status": "open"}, {"status": "closed"}, {"status": "all"}, {"status": "open,closed"}):
        assert opportunity_status_chosen(chosen), chosen
        assert opportunity_status_form_value(chosen) == chosen["status"]


def test_the_standfirst_names_the_statuses_in_view() -> None:
    assert opportunity_status_phrase({}) == "Open and announced"
    assert opportunity_status_phrase({"status": "closed"}) == "Closed"
    assert opportunity_status_phrase({"status": "all"}) == "All"
    assert opportunity_status_phrase({"status": "open,awarded,closed"}) == "Open, awarded and closed"


def test_the_window_is_the_last_365_days_to_now() -> None:
    start, end = recently_closed_window(dt.datetime(2026, 10, 23, 9, 30, 15, 123456, tzinfo=dt.UTC))
    assert (start, end) == ("2025-10-23T09:30:15Z", "2026-10-23T09:30:15Z")


def test_an_announced_notice_names_its_opening_date_only_while_it_is_ahead() -> None:
    today = dt.date(2026, 10, 23)
    assert announced_opens_on("announced", "2026-11-02", today) == "2026-11-02"
    assert announced_opens_on("announced", "2026-10-23", today) == "2026-10-23"
    assert announced_opens_on("announced", "2026-10-01", today) is None  # past: never "opens" in the past
    assert announced_opens_on("announced", None, today) is None
    assert announced_opens_on("announced", "not a date", today) is None
    assert announced_opens_on("open", "2026-11-02", today) is None


# ------------------------------------------------------------------------------- the page
def test_the_default_view_asks_for_open_and_announced_and_marks_announced_rows(client: TestClient) -> None:
    fake = _install(Store([OPEN, ANNOUNCED, *CLOSED], CLOSED))
    html = client.get("/opportunities").text
    main = _main_call(fake)
    assert main["status"] == "open,announced" and main["relevance"] == "energy"
    table = _table(html)
    # Both rows, and no closed row in the list itself.
    assert "/opportunities/all-source-rfp" in table and "/opportunities/zero-carbon-draft-rfp" in table
    assert "closed-rfp-" not in table and ">Closed<" not in table
    announced_row = re.search(r"<tr>(?:(?!</tr>).)*zero-carbon-draft-rfp.*?</tr>", table, re.S)
    assert announced_row
    row = announced_row.group(0)
    assert '<span class="chip__label">Announced</span>' in row
    assert "Not open yet: opens 15 Jan 2099" in _text(row)
    open_row = re.search(r"<tr>(?:(?!</tr>).)*all-source-rfp.*?</tr>", table, re.S)
    assert open_row and "Not open yet" not in open_row.group(0)
    # The standfirst says what the view holds.
    assert "Open and announced energy notices, soonest deadline first" in _text(html)


def test_recently_closed_follows_the_list_newest_first_with_its_count_link_and_credit(
    client: TestClient,
) -> None:
    fake = _install(Store([OPEN, ANNOUNCED], CLOSED[:10], closed_total=12))
    html = client.get("/opportunities?jurisdiction=US-CT&due_at[from]=2026-11-01").text
    [closed_call] = _closed_calls(fake)
    start, end = (
        dt.datetime.fromisoformat(closed_call[k].replace("Z", "+00:00"))
        for k in ("due_at[from]", "due_at[to]")
    )
    assert closed_call["status"] == "closed" and closed_call["sort"] == "-due_at"
    assert closed_call["limit"] == 10 and closed_call["include"] == "count"
    assert closed_call["relevance"] == "energy" and closed_call["jurisdiction"] == "US-CT"
    assert (end - start).days == 365 and abs((dt.datetime.now(dt.UTC) - end).total_seconds()) < 120
    # The section's own window replaces a deadline filter the reader set for the list.
    assert _main_call(fake)["due_at[from]"] == "2026-11-01"
    # After the table, never inside it.
    assert html.index("</table>") < html.index('<section class="recent-closed"')
    section = _section(html)
    assert '<h2 id="recent-closed-title" class="recent-closed__title">Recently closed' in section
    assert "Recently closed (last 12 months)" in _text(section)
    slugs = re.findall(r'href="/opportunities/(closed-rfp-\d+)"', section)
    assert slugs == [f"closed-rfp-{i}" for i in range(10)]  # as the API ordered them, newest first
    assert section.count('<span class="chip__label">Closed</span>') == 10
    assert "chip--success" not in section and ">Open<" not in section
    assert "Deadline 28 Sep 2026" in _text(section)
    assert "10 of 12 closed in the last 12 months." in _text(section)
    more = re.search(r'<a href="([^"]+)">All closed notices', section)
    assert more
    assert parse_qsl(urlsplit(unescape(more.group(1))).query) == [
        ("jurisdiction", "US-CT"),
        ("due_at[from]", "2026-11-01"),
        ("status", "closed"),
        ("sort", "-due_at"),
    ]
    # Its own credit line names the register behind its rows.
    assert "Sources of these rows: " in section and "Public Service Company of Oklahoma" in section


def test_the_section_never_shows_a_row_the_api_answers_with_another_status(client: TestClient) -> None:
    _install(Store([OPEN], [OPEN, ANNOUNCED, CLOSED[0]]))
    section = _section(client.get("/opportunities").text)
    assert re.findall(r'href="/opportunities/([a-z0-9-]+)"', section) == ["closed-rfp-0"]
    # Only open and announced rows: no section at all.
    _install(Store([OPEN], [OPEN, ANNOUNCED]))
    assert 'class="recent-closed"' not in client.get("/opportunities").text


@pytest.mark.parametrize(
    "query", ["status=open", "status=closed", "status=all", "status=open%2Cclosed", "cursor=abc"]
)
def test_no_section_on_a_chosen_status_or_a_later_page(client: TestClient, query: str) -> None:
    fake = _install(Store([OPEN, ANNOUNCED, *CLOSED], CLOSED))
    html = client.get(f"/opportunities?{query}").text
    assert 'class="recent-closed"' not in html
    assert _closed_calls(fake) == []


def test_the_default_named_in_the_url_is_still_the_default(client: TestClient) -> None:
    fake = _install(Store([OPEN, ANNOUNCED], CLOSED))
    html = client.get("/opportunities?status=announced%2Copen&relevance=all").text
    assert 'class="recent-closed"' in html and len(_closed_calls(fake)) == 1
    assert "relevance" not in _closed_calls(fake)[0]  # the all-notices scope reaches the section too
    assert 'rel="canonical" href="http://testserver/opportunities?relevance=all"' in html
    assert 'class="filter-bar__clear"' not in html


def test_the_status_select_opens_on_the_default_and_a_choice_is_kept(client: TestClient) -> None:
    _install(Store([OPEN, ANNOUNCED, *CLOSED], CLOSED))
    html = client.get("/opportunities").text
    select = html.split('<select id="f-status" name="status"')[1].split("</select>")[0]
    assert select.startswith(' data-default="open,announced">')
    options = re.findall(r'<option value="([^"]*)" ?(selected)?>([^<]*)</option>', select)
    assert options[0] == ("open,announced", "selected", "Open and announced")
    assert [o[0] for o in options] == ["open,announced", "all", "unknown", "announced", "open", "closed"]
    assert 'class="filter-bar__clear"' not in html
    chosen = client.get("/opportunities?status=closed").text
    select = chosen.split('<select id="f-status" name="status"')[1].split("</select>")[0]
    assert re.search(r'<option value="closed" selected>Closed</option>', select)
    assert 'class="filter-bar__clear"' in chosen
    assert "Closed energy notices, soonest deadline first" in _text(chosen)


def test_the_feed_and_the_alert_watch_what_the_page_shows(client: TestClient) -> None:
    _install(Store([OPEN, ANNOUNCED], []))
    html = client.get("/opportunities?kind=rfp").text
    feed = re.search(r'<a href="([^"]+)" type="application/rss\+xml">', html)
    assert feed and dict(parse_qsl(urlsplit(unescape(feed.group(1))).query))["status"] == "open,announced"
    alert = re.search(r'<a href="(/alerts/new\?[^"]+)">Save this search', html)
    assert alert
    from web.alerts import saved_search_query

    query = dict(parse_qsl(urlsplit(unescape(alert.group(1))).query))
    assert saved_search_query("opportunity", query)["status"] == "open,announced"


def test_an_htmx_filter_change_swaps_the_section_with_the_rows(client: TestClient) -> None:
    _install(Store([OPEN], CLOSED[:3]))
    partial = client.get("/opportunities?status=open%2Cannounced", headers={"HX-Request": "true"}).text
    assert "<html" not in partial and 'class="recent-closed"' in partial
    assert partial.index("</table>") < partial.index('class="recent-closed"')


def test_a_failed_section_leaves_the_page_as_it_was(client: TestClient) -> None:
    store = Store([OPEN], CLOSED)
    store.closed_answer = (503, {"title": "unavailable"})
    _install(store)
    response = client.get("/opportunities")
    assert response.status_code == 200
    assert "/opportunities/all-source-rfp" in response.text and 'class="recent-closed"' not in response.text


# ------------------------------------------------------------------------------- empty states
def test_with_nothing_open_announced_or_recently_closed_the_page_says_why_and_where_next(
    client: TestClient,
) -> None:
    _install(Store([], []))
    html = client.get("/opportunities").text
    box = re.search(r'<div class="empty-state rfp-empty">.*?</div>', html, re.S)
    assert box
    text = _text(box.group(0))
    assert text.startswith("No open RFPs from the issuers we read right now.")
    assert "Utility RFPs are rare and stay open for a few weeks" in text
    found = re.findall(r'<a href="([^"]+)">([^<]+)</a>', box.group(0))
    links = {label: unescape(href) for href, label in found}
    assert links["Closed notices"] == "/opportunities?status=closed&sort=-due_at"
    assert links["The issuers we read"] == "/about#sources"
    assert links["Get an alert when one opens"].startswith("/alerts/new?entity=opportunity")
    assert "Clear all filters" not in text  # nothing to clear
    assert 'class="recent-closed"' not in html


def test_with_nothing_open_the_recently_closed_still_fill_the_page(client: TestClient) -> None:
    """The production store of 2026-10-23 less its announced notice: the list is empty, the closed
    notices of the year follow, and nothing claims any of them is open."""
    _install(Store([*CLOSED], CLOSED[:10], closed_total=12))
    html = client.get("/opportunities").text
    box = re.search(r'<div class="empty-state rfp-empty">.*?</div>', html, re.S)
    assert box
    text = _text(box.group(0))
    assert text.startswith("No open RFPs from the issuers we read right now.")
    assert "Closed notices" not in text  # the section below has the closed list's link
    assert "The issuers we read" in text
    assert html.index('class="empty-state rfp-empty"') < html.index('<section class="recent-closed"')
    assert _section(html).count('<span class="chip__label">Closed</span>') == 10
    assert '<span class="chip__label">Open</span>' not in html


def test_a_filtered_empty_view_names_the_filter_and_keeps_its_way_out(client: TestClient) -> None:
    _install(Store([], []))
    text = _text(client.get("/opportunities?jurisdiction=US-ZZ").text)
    assert "No open or announced energy-related notices match these filters right now." in text
    assert "Clear all filters" in text and "No open RFPs from the issuers" not in text
    chosen = _text(client.get("/opportunities?status=open&jurisdiction=US-ZZ").text)
    assert "No open energy-related opportunities match these filters." in chosen
