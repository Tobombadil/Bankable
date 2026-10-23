"""The companies index and the company page header (lane pages, 2026-10-23; docs/16 U-9).

Held here, against a hand-written fake of the API (no database), as `web/test_seo.py` does:

- `/organizations` reads one list call per page and prints each row's stored figures (`stats`) in
  the units the page names (GW, to sensible precision), each a link to the list that reproduces it:
  proposals on `/proposals`, RFPs on `/opportunities` over all notices, assets on the company page
  narrowed to the same statuses and role; a missing figure is a dash, a zero is muted;
- the default order is operating capacity, largest first, over whole groups; a search with no chosen
  order keeps the API's best match; a heading click sorts and reverses with the filters kept; a sort
  the page does not offer falls back and is not written into the form;
- the Whole group / This company only switch, the filters it forwards, the Type filter offered only
  when a type other than "other" has companies, and the "figures refreshing" state (re-read in name
  order, dashes not zeros, an empty figure filter explained);
- the company page header: description, website, headquarters and listing, each with its source
  line, the LEI and Wikipedia link in the fields, no duplicate website, nothing for a missing fact;
- the company page's Assets section narrowed by a figure's `status` and `role`, with the count.

The fake is duplicated rather than imported, per this repo's convention that no `web/test_*.py`
imports another.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.organizations import (
    asset_filter_notice,
    figure_href,
    gw_text,
    org_index_row,
    organization_profile_view,
)

ORG_ID = "org_duke"
VOCAB: dict[str, Any] = {
    "technology": [{"value": "solar"}, {"value": "wind"}],
    "organization_type": [{"value": "developer"}, {"value": "utility"}, {"value": "other"}],
    "proposal_kind": [{"value": "generation"}],
    "opportunity_kind": [{"value": "rfp"}],
    "opportunity_status": [{"value": "open"}],
}
ACTIVE = "announced,filed,studied,permitted,contracted,under_construction"


class FakeTransport:
    """Canned responses keyed by path; a response may be a callable of the call's params. Every
    call is recorded so a test can assert what a route forwarded."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _respond(self, url: str, params: dict[str, Any]) -> httpx.Response:
        canned = self.responses.get(url)
        if canned is None:
            return httpx.Response(404, json={"title": "not_found", "detail": url})
        if callable(canned):
            canned = canned(params)
        status, body = canned
        return httpx.Response(status, json=body)

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, **_: Any) -> httpx.Response:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        self.calls.append((url, clean))
        return self._respond(url, clean)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return self._respond(url, {})

    def request(
        self, method: str, url: str, *, params: Mapping[str, Any] | None = None, **_: Any
    ) -> httpx.Response:
        return self.get(url, params=params)

    def close(self) -> None:
        pass

    def params_for(self, url: str, **match: str) -> list[dict[str, Any]]:
        return [p for u, p in self.calls if u == url and all(p.get(k) == v for k, v in match.items())]


@pytest.fixture()
def client() -> Iterator[TestClient]:
    _reset()
    with TestClient(web_app) as c:
        yield c
    _reset()


def _reset() -> None:
    for key in (
        "api_client",
        "lag_days_default",
        "coverage_facts",
        "build_info",
        "sitemap_cache",
        "org_types_present",
    ):
        web_app.state.__dict__.pop(key, None)


def _install(responses: Mapping[str, Any]) -> FakeTransport:
    transport = FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/meta/vocabularies": (200, {"data": VOCAB}),
            "/v1/sources": (200, {"data": []}),
            "/v1/coverage": (200, {"data": {}}),
            **responses,
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _queries(scope: str = "all") -> dict[str, Any]:
    assets = {
        "path": f"/v1/organizations/{ORG_ID}/assets",
        "query": {"scope": scope, "status": "operating,standby,retiring"},
    }
    owned = {"path": assets["path"], "query": {**assets["query"], "role": "owner"}}
    if scope != "all":
        assets = {"path": assets["path"], "query": {"status": "operating,standby,retiring"}}
        owned = {"path": assets["path"], "query": {"status": "operating,standby,retiring", "role": "owner"}}
    pipeline = {
        "path": "/v1/proposals",
        "query": {"sponsor_id": ORG_ID, "sponsor_scope": "all", "lifecycle_state": ACTIVE, "listed": "true"},
    }
    return {
        "assets": assets,
        "operating_mw": assets,
        "owned_assets": owned,
        "owned_mw": owned,
        "pipeline_count": pipeline,
        "pipeline_mw": pipeline,
        "rfps_issued": {"path": "/v1/opportunities", "query": {"issuer_id": ORG_ID, "status": "open,closed"}},
    }


def _stats(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scope": "group",
        "organizations": 4,
        "assets": 41,
        "operated_assets": 0,
        "owned_assets": 41,
        "operating_mw": 43350.6,
        "operating_mw_assets": 41,
        "owned_mw": 40672.082,
        "owned_mw_assets": 40,
        "pipeline_count": 21,
        "pipeline_mw": 998.9,
        "pipeline_mw_records": 21,
        "withdrawn_count": 0,
        "proposals": 21,
        "rfps_issued": 0,
        "rfps_open": 0,
        "states": ["US-FL", "US-IN", "US-KY", "US-NC", "US-OH", "US-SC"],
        "isos": ["DUK"],
        "technologies": ["coal", "solar"],
        "list_queries": _queries(),
        "sources": [{"source_id": "us.eia.860m", "records": 70, "retrieved_at_max": "2026-10-09T19:51:59Z"}],
        "computed_at": "2026-10-23T00:34:54Z",
    }
    base.update(overrides)
    return base


def _org(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": ORG_ID,
        "slug": "duke-energy",
        "name_canonical": "Duke Energy Corp",
        "type": "other",
        "country": "US",
        "jurisdiction": None,
        "ids": {},
        "website": None,
        "personal_data": False,
        "provenance": [],
        "stats": _stats(),
    }
    base.update(overrides)
    return base


def _list(
    rows: list[dict[str, Any]], *, state: str = "current", scope: str = "group", sources: Any = None
) -> Any:
    return (
        200,
        {
            "data": rows,
            "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
            "meta": {
                "total": len(rows),
                "total_is_estimate": False,
                "organization_stats": {"scope": scope, "tier": "public", "state": state},
            },
            "licence_summary": {"sources": sources if sources is not None else []},
        },
    )


def _orgs_route(rows: list[dict[str, Any]], **kwargs: Any) -> Callable[[dict[str, Any]], Any]:
    """`/v1/organizations`: the rows for the page's read, one row for a Type probe that names a type
    the rows have, none otherwise."""

    def respond(params: dict[str, Any]) -> Any:
        if "type" in params and params.get("limit") == "1":
            return _list([r for r in rows if r.get("type") in params["type"].split(",")][:1])
        return _list(rows, **kwargs)

    return respond


def _row_html(body: str, name: str) -> str:
    match = re.search(
        r"<tr>\s*<td data-label=\"Company\"[^>]*>.*?" + re.escape(name) + r".*?</tr>", body, re.S
    )
    assert match, f"no row for {name}"
    return match.group(0)


def _hrefs(fragment: str) -> list[str]:
    return [unquote(html.unescape(h)) for h in re.findall(r'class="figure-link" href="([^"]+)"', fragment)]


# ------------------------------------------------------------------------------- figures in units
@pytest.mark.parametrize(
    ("mw", "text"),
    [
        (None, None),
        (0, "0"),
        (4.0, "<0.01"),
        (12.0, "0.01"),
        (250.0, "0.25"),
        (1500.0, "1.50"),
        (9994.0, "9.99"),
        (9999.9, "10.0"),  # rounds to ten: one decimal, not "10.00"
        (43350.6, "43.4"),
    ],
)
def test_capacity_prints_in_gw_to_a_precision_that_reads(mw: Any, text: str | None) -> None:
    assert gw_text(mw) == text


def test_a_row_without_figures_has_every_figure_missing_never_zero() -> None:
    row = org_index_row(_org(stats=None), scope="group")
    assert row["figures"] == dict.fromkeys(
        ("assets", "operating_mw", "owned_mw", "pipeline_count", "pipeline_mw")
    )


def test_each_figure_links_to_the_list_whose_total_it_is() -> None:
    record = {"slug": "duke-energy", "public_id": ORG_ID}
    queries = _queries()
    assert figure_href(record, queries["pipeline_count"], scope="group") == (
        f"/proposals?sponsor_id={ORG_ID}&sponsor_scope=all&lifecycle_state="
        "announced%2Cfiled%2Cstudied%2Cpermitted%2Ccontracted%2Cunder_construction&listed=true"
    )
    # Every notice the issuer has, not the energy-related default view of the RFPs page.
    assert figure_href(record, queries["rfps_issued"], scope="group") == (
        f"/opportunities?issuer_id={ORG_ID}&status=open%2Cclosed&relevance=all"
    )
    # No page lists one company's assets but its own: the Assets section at the same statuses and
    # role, at the page's default scope for a group and `scope=self` for the company alone.
    assert figure_href(record, queries["owned_mw"], scope="group") == (
        "/organizations/duke-energy?status=operating%2Cstandby%2Cretiring&role=owner#assets"
    )
    assert figure_href(record, _queries("self")["assets"], scope="self") == (
        "/organizations/duke-energy?scope=self&status=operating%2Cstandby%2Cretiring#assets"
    )
    assert figure_href(record, {"path": "/v1/elsewhere", "query": {}}, scope="group") is None
    assert figure_href(record, None, scope="group") is None


# ------------------------------------------------------------------------------- /organizations
def test_the_index_opens_on_whole_groups_largest_operating_fleet_first(client: TestClient) -> None:
    smaller = _org(
        public_id="org_small",
        slug="small-solar",
        name_canonical="Small Solar LLC",
        stats=_stats(operating_mw=4.0),
    )
    transport = _install({"/v1/organizations": _orgs_route([_org(), smaller])})

    body = client.get("/organizations").text

    page_read = transport.params_for("/v1/organizations", include="count")
    assert len(page_read) == 1
    assert page_read[0] | {"cursor": None} == {
        "scope": "group",
        "sort": "-operating_mw",
        "limit": "50",
        "include": "count",
        "cursor": None,
    }
    # One read per page: no row needs its own detail call any more.
    assert not [u for u, _ in transport.calls if u.startswith(f"/v1/organizations/{ORG_ID}")]
    assert "2 companies, sorted by operating capacity, largest first." in body
    assert re.search(
        r'class="sortable num is-sorted" aria-sort="descending">'
        r'<a class="sort-link" href="[^"]*sort=operating_mw"',
        body,
    )
    row = _row_html(body, "Duke Energy Corp")
    assert (
        ">43.4</a>" in row
        and ">40.7</a>" in row
        and ">21</a>" in row
        and ">1.00</a>" in row
        and ">41</a>" in row
    )
    assert 'title="43,350.6 MW across 41 assets with a stated capacity."' in row
    assert "Group of 4 companies · FL, IN, KY, NC +2" in row
    assert "&lt;0.01" in _row_html(body, "Small Solar LLC")
    assert _hrefs(row) == [
        "/organizations/duke-energy?status=operating,standby,retiring#assets",
        "/organizations/duke-energy?status=operating,standby,retiring#assets",
        "/organizations/duke-energy?status=operating,standby,retiring&role=owner#assets",
        f"/proposals?sponsor_id={ORG_ID}&sponsor_scope=all&lifecycle_state={ACTIVE}&listed=true",
        f"/proposals?sponsor_id={ORG_ID}&sponsor_scope=all&lifecycle_state={ACTIVE}&listed=true",
    ]


def test_a_missing_figure_is_a_dash_and_a_zero_is_muted_and_off_the_phone_row(client: TestClient) -> None:
    stats = _stats(pipeline_count=0, pipeline_mw=None, pipeline_mw_records=0)
    _install({"/v1/organizations": _orgs_route([_org(stats=stats)])})

    row = _row_html(client.get("/organizations").text, "Duke Energy Corp")

    assert re.search(r'cell-figure cell-opt"><span class="figure-zero">0</span>', row)  # projects
    assert re.search(r'cell-figure is-empty">—</td>', row)  # development GW


def test_a_search_keeps_best_match_order_and_marks_no_heading(client: TestClient) -> None:
    transport = _install({"/v1/organizations": _orgs_route([_org()])})

    body = client.get("/organizations", params={"q": "duke"}).text

    (read,) = transport.params_for("/v1/organizations", include="count")
    assert read["q"] == "duke" and "sort" not in read
    assert "1 company matching &ldquo;duke&rdquo;, best matches first." in body
    assert "aria-sort" not in body
    # The name box belongs to the filter form, so one GET carries the name and every filter.
    assert re.search(r'<input id="f-org-q"[^>]*form="filters"[^>]*name="q"[^>]*value="duke"', body)


def test_a_heading_sorts_and_reverses_keeping_the_filters(client: TestClient) -> None:
    transport = _install({"/v1/organizations": _orgs_route([_org()])})

    body = client.get("/organizations", params={"state": "US-TX", "sort": "-pipeline_count"}).text

    (read,) = transport.params_for("/v1/organizations", include="count")
    assert read["sort"] == "-pipeline_count" and read["state"] == "US-TX"
    links = dict(re.findall(r'href="(/organizations\?[^"]+)" title="Sort by ([^"]+)"', body))
    assert (
        links["/organizations?state=US-TX&amp;sort=pipeline_count"]
        == "projects in development, smallest first"
    )
    assert links["/organizations?state=US-TX&amp;sort=-owned_mw"] == "owned capacity, largest first"
    assert links["/organizations?state=US-TX&amp;sort=name"] == "name, A to Z"
    assert '<input type="hidden" name="sort" value="-pipeline_count">' in body
    assert '<option value="US-TX" selected>Texas</option>' in body


def test_a_sort_the_page_does_not_offer_falls_back_and_stays_out_of_the_form(client: TestClient) -> None:
    transport = _install({"/v1/organizations": _orgs_route([_org()])})

    body = client.get("/organizations", params={"sort": "-favourite"}).text

    (read,) = transport.params_for("/v1/organizations", include="count")
    assert read["sort"] == "-operating_mw"
    assert 'name="sort"' not in body


def test_the_switch_counts_the_company_alone_and_keeps_that_everywhere(client: TestClient) -> None:
    alone = _stats(scope="self", organizations=1, list_queries=_queries("self"))
    transport = _install({"/v1/organizations": _orgs_route([_org(stats=alone)], scope="self")})

    body = client.get("/organizations", params={"scope": "self", "role": "owner"}).text

    (read,) = transport.params_for("/v1/organizations", include="count")
    assert read["scope"] == "self" and read["role"] == "owner"
    assert (
        '<a class="view-switch__link" id="scope-self" '
        'href="/organizations?role=owner&amp;scope=self" rel="nofollow" aria-current="page">' in body
    )
    assert (
        '<a class="view-switch__link" id="scope-group" href="/organizations?role=owner">Whole group</a>'
        in body
    )
    assert '<input type="hidden" name="scope" value="self">' in body
    row = _row_html(body, "Duke Energy Corp")
    assert _hrefs(row)[0] == "/organizations/duke-energy?scope=self&status=operating,standby,retiring#assets"
    assert "Group of" not in row


def test_the_filters_reach_the_api_and_unknown_parameters_do_not(client: TestClient) -> None:
    transport = _install({"/v1/organizations": _orgs_route([_org()])})

    body = client.get(
        "/organizations",
        params={
            "state": "US-NC",
            "technology": "solar",
            "role": "sponsor",
            "has_pipeline": "true",
            "colour": "red",
        },
    ).text

    (read,) = transport.params_for("/v1/organizations", include="count")
    assert {k: read[k] for k in ("state", "technology", "role", "has_pipeline")} == {
        "state": "US-NC",
        "technology": "solar",
        "role": "sponsor",
        "has_pipeline": "true",
    }
    assert "colour" not in read
    # A state or technology picks companies; the row still prints the company's whole figures.
    assert "The filters pick the companies; the figures are each one&rsquo;s totals." in body
    assert "figures are each one" not in client.get("/organizations").text


def test_the_type_filter_is_offered_only_for_types_with_companies(client: TestClient) -> None:
    _install({"/v1/organizations": _orgs_route([_org(type="other")])})
    assert 'id="f-org-type"' not in client.get("/organizations").text  # only "other": no menu

    _reset()
    _install(
        {"/v1/organizations": _orgs_route([_org(type="other"), _org(public_id="org_u", type="utility")])}
    )
    body = client.get("/organizations").text
    options = re.findall(r'<select id="f-org-type".*?</select>', body, re.S)[0]
    assert '<option value="utility" >Utility</option>' in options
    assert 'value="developer"' not in options  # no developer in the store


def test_while_figures_refresh_the_list_is_read_in_name_order_and_says_so(client: TestClient) -> None:
    stale = [_org(stats=None), _org(public_id="org_b", slug="b", name_canonical="B Power", stats=None)]
    transport = _install({"/v1/organizations": _orgs_route(stale, state="stale")})

    body = client.get("/organizations").text

    sorts = [p.get("sort") for p in transport.params_for("/v1/organizations", include="count")]
    assert sorts == ["-operating_mw", "name"]
    assert "Company figures are being refreshed after a data update" in body
    assert "2 companies, sorted by name, A to Z." in body
    assert 'class="figure-link"' not in body and ">0<" not in body


def test_a_figure_filter_that_matches_nothing_while_refreshing_says_why(client: TestClient) -> None:
    _install({"/v1/organizations": _orgs_route([], state="missing")})

    body = client.get("/organizations", params={"has_pipeline": "true"}).text

    assert "the filters on them match nothing until they are back" in body


def test_the_credit_line_names_the_registers_behind_the_figures(client: TestClient) -> None:
    sources = [
        {"source_id": "us.eia.860m", "name": "EIA-860M", "attribution_text": None, "reuse_class": "open"}
    ]
    _install({"/v1/organizations": _orgs_route([_org()], sources=sources)})

    body = client.get("/organizations").text

    assert re.search(r"Sources of these rows: EIA-860M[^.]*\. Licences and full credits", body)


def test_without_a_register_behind_the_page_the_credit_line_says_where_names_come_from(
    client: TestClient,
) -> None:
    _install({"/v1/organizations": _orgs_route([_org(stats=None)])})

    assert "Companies are named as the source registers" in client.get("/organizations").text


# --------------------------------------------------------- hand-typed and stale links (fix round)
#: What `GET /v1/organizations` refuses with a 400 (services/api/organizations.py `_stats_filters`).
_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _number_at_least_zero(value: str) -> bool:
    try:
        number = float(value)
    except ValueError:
        return False
    return math.isfinite(number) and number >= 0


API_CHECKS: dict[str, Callable[[str], bool]] = {
    "role": lambda v: all(t in ("operator", "owner", "sponsor") for t in v.split(",")),
    "has_pipeline": lambda v: v in ("true", "false"),
    "has_assets": lambda v: v in ("true", "false"),
    "min_operating_mw": _number_at_least_zero,
    "state": lambda v: all(_TOKEN.match(t) for t in v.split(",")),
    "iso": lambda v: all(_TOKEN.match(t) for t in v.split(",")),
    "technology": lambda v: all(_TOKEN.match(t) for t in v.split(",")),
}
FILTER_NAMES = (
    "q",
    "role",
    "has_pipeline",
    "has_assets",
    "min_operating_mw",
    "state",
    "iso",
    "technology",
    "is_curated_issuer",
)


def _strict_orgs_route(rows: list[dict[str, Any]]) -> Callable[[dict[str, Any]], Any]:
    """`/v1/organizations` as strict as the real one: a value it refuses is a 400 naming the field."""
    lenient = _orgs_route(rows)

    def respond(params: dict[str, Any]) -> Any:
        for name, ok in API_CHECKS.items():
            if name in params and not ok(str(params[name])):
                return 400, {"code": "validation_error", "title": "Invalid", "errors": [{"field": name}]}
        return lenient(params)

    return respond


@pytest.mark.parametrize(
    ("params", "kept"),
    [
        ({"role": "boss"}, {}),
        ({"role": "Owner,boss"}, {"role": "owner"}),
        ({"has_pipeline": "maybe"}, {}),
        ({"has_pipeline": "TRUE"}, {"has_pipeline": "true"}),
        ({"has_assets": "1"}, {}),
        ({"min_operating_mw": "abc"}, {}),
        ({"min_operating_mw": "-5"}, {}),
        ({"min_operating_mw": "nan"}, {}),
        ({"min_operating_mw": "500"}, {"min_operating_mw": "500"}),
        ({"iso": "ER%"}, {}),
        ({"state": "US-TX|,US-OK"}, {"state": "US-OK"}),
        ({"technology": "sol ar"}, {}),
        ({"is_curated_issuer": "maybe"}, {}),
        ({"q": "   "}, {}),
    ],
)
def test_a_filter_value_the_api_would_refuse_is_dropped_not_a_server_error(
    client: TestClient, params: dict[str, str], kept: dict[str, str]
) -> None:
    """A hand-typed or stale link degrades to the list without that filter (the `resolve_scope`
    rule), as `/proposals` and `/opportunities` do; before the fix each of these was a 500."""
    transport = _install({"/v1/organizations": _strict_orgs_route([_org()])})

    resp = client.get("/organizations", params=params)

    assert resp.status_code == 200
    (read,) = transport.params_for("/v1/organizations", include="count")
    assert {k: read[k] for k in FILTER_NAMES if k in read} == kept
    # What was dropped is not echoed back into the form, the links or the canonical URL.
    text = unquote(html.unescape(resp.text))
    for name, value in params.items():
        if value.strip() and kept.get(name) != value:
            assert f"{name}={value}" not in text and f'name="{name}" value="{value}"' not in text


def test_a_search_of_only_spaces_is_no_search(client: TestClient) -> None:
    transport = _install({"/v1/organizations": _orgs_route([_org()])})

    body = client.get("/organizations", params={"q": "  "}).text

    (read,) = transport.params_for("/v1/organizations", include="count")
    assert "q" not in read and read["sort"] == "-operating_mw"  # the default order, not best match
    assert "1 company, sorted by operating capacity, largest first." in body
    assert "matching" not in re.search(r'<p class="result-count".*?</p>', body, re.S).group(0)  # type: ignore[union-attr]


def test_a_refusal_the_checks_did_not_foresee_drops_that_parameter_and_reads_again(
    client: TestClient,
) -> None:
    """A rule the API adds later (here a state it does not know), and a cursor that has expired:
    the page reads again without what the 400 names, page one for the cursor."""

    def respond(params: dict[str, Any]) -> Any:
        if params.get("state") == "US-ZZ":
            return 400, {"code": "validation_error", "title": "Invalid", "errors": [{"field": "state"}]}
        if params.get("cursor") == "expired":
            return 400, {"code": "invalid_cursor", "title": "Cursor expired or malformed"}
        return _list([_org()])

    transport = _install({"/v1/organizations": respond})

    resp = client.get("/organizations", params={"state": "US-ZZ", "role": "owner", "cursor": "expired"})

    assert resp.status_code == 200
    reads = transport.params_for("/v1/organizations", include="count")
    assert [("state" in r, "cursor" in r) for r in reads] == [(True, True), (False, True), (False, False)]
    assert reads[-1]["role"] == "owner"
    assert "US-ZZ" not in resp.text and "expired" not in resp.text
    assert '<option value="owner" selected>' in resp.text


def test_a_refusal_that_names_nothing_the_page_sent_is_still_an_error(client: TestClient) -> None:
    transport = _install(
        {"/v1/organizations": (400, {"code": "validation_error", "errors": [{"field": "limit_zz"}]})}
    )
    with pytest.raises(Exception, match="API 400"):
        client.get("/organizations")
    assert len(transport.params_for("/v1/organizations", include="count")) == 1


@pytest.mark.parametrize(
    ("params", "canonical"),
    [
        ({"scope": "group", "sort": "-operating_mw"}, "/organizations"),
        ({"scope": "bogus", "sort": "bogus"}, "/organizations"),
        (
            {"role": "boss", "state": "US-TX", "scope": "self", "sort": "-assets", "utm_source": "x"},
            "/organizations?state=US-TX&scope=self&sort=-assets",
        ),
        # While searching, the default is best match, so the operating-capacity order is a choice.
        ({"q": "duke", "sort": "-operating_mw"}, "/organizations?q=duke&sort=-operating_mw"),
    ],
)
def test_the_canonical_url_drops_defaults_and_values_the_page_does_not_use(
    client: TestClient, params: dict[str, str], canonical: str
) -> None:
    _install({"/v1/organizations": _strict_orgs_route([_org()])})

    body = client.get("/organizations", params=params).text

    expected = html.escape(f"http://testserver{canonical}", quote=True)
    assert f'<link rel="canonical" href="{expected}">' in body


def test_the_company_alone_view_is_not_a_second_crawl_path(client: TestClient) -> None:
    _install({"/v1/organizations": _orgs_route([_org()])})

    body = client.get("/organizations").text

    assert re.search(r'<a class="view-switch__link" id="scope-self" href="[^"]+" rel="nofollow"', body)
    assert not re.search(r'id="scope-group" href="[^"]+" rel="nofollow"', body)  # the canonical view


def test_the_type_menu_costs_one_read_when_every_company_is_other(client: TestClient) -> None:
    transport = _install({"/v1/organizations": _orgs_route([_org(type="other")])})

    client.get("/organizations")

    probes = [p for p in transport.params_for("/v1/organizations", limit="1") if "type" in p]
    assert probes == [{"type": "developer,utility", "limit": "1"}]


def test_while_figures_refresh_the_next_page_keeps_the_name_order_its_cursor_belongs_to(
    client: TestClient,
) -> None:
    def respond(params: dict[str, Any]) -> Any:
        status, body = _list([_org(stats=None)], state="stale")
        body["page"] = {"has_more": True, "next_cursor": "c2", "prev_cursor": None}
        return status, body

    _install({"/v1/organizations": respond})

    body = client.get("/organizations").text

    pager = _first(r'<nav class="pager".*?</nav>', body)
    assert 'href="/organizations?sort=name&cursor=c2"' in pager


def test_the_refreshing_note_names_the_order_the_rows_are_in(client: TestClient) -> None:
    stale = [_org(stats=None)]
    _install({"/v1/organizations": _orgs_route(stale, state="stale")})

    by_name = client.get("/organizations").text
    searched = client.get("/organizations", params={"q": "duke"}).text

    note = r'<p class="view-notice" role="status" id="figures-refreshing">(.*?)</p>'
    by_name_note = re.search(note, by_name, re.S).group(1)  # type: ignore[union-attr]
    searched_note = re.search(note, searched, re.S).group(1)  # type: ignore[union-attr]
    assert "the list is in name order" in by_name_note
    assert "name order" not in searched_note and "the best matches come first" in searched_note
    # The figures come back on the org-stats tick (every 5 minutes) or the next refresh.
    assert "within a few minutes" in by_name_note and "within a minute" not in by_name_note


def test_a_capacity_few_of_the_assets_state_says_how_few(client: TestClient) -> None:
    """A pipeline operator with 47 assets and one plant's capacity stated is not a 0.01 GW company."""
    stats = _stats(assets=47, operating_mw=12.0, operating_mw_assets=1, owned_assets=0, owned_mw=None)
    _install({"/v1/organizations": _orgs_route([_org(stats=stats)])})

    row = _row_html(client.get("/organizations").text, "Duke Energy Corp")

    assert '<span class="figure-note"> from 1 of 47 assets</span>' in row
    assert 'title="12 MW across 1 of 47 assets; the others state no capacity."' in row
    # Every asset states a capacity: no note.
    assert "figure-note" not in _row_html_for(client, _stats())


def _row_html_for(client: TestClient, stats: dict[str, Any]) -> str:
    _reset()
    _install({"/v1/organizations": _orgs_route([_org(stats=stats)])})
    return _row_html(client.get("/organizations").text, "Duke Energy Corp")


# ---------------------------------------------------------------------------- the company page
WIKIDATA = {
    "source_id": "global.wikidata",
    "source_name": "Wikidata — company items",
    "source_url": "https://www.wikidata.org/wiki/Q1264404#P856",
    "retrieved_at": "2026-10-22T12:00:00Z",
    "licence_id": "global.wikidata#x",
    "reuse_class": "open",
    "attribution_text": "Source: Wikidata (CC0)",
}
SEC = {
    **WIKIDATA,
    "source_id": "us.sec.company_tickers",
    "source_url": "https://www.sec.gov/edgar/browse/?CIK=1326160",
    "attribution_text": "Source: U.S. Securities and Exchange Commission, EDGAR",
}


def _profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "website": {"value": "https://www.duke-energy.com/", "provenance": WIKIDATA},
        "headquarters": {"value": {"city": "Charlotte", "country": "US"}, "provenance": WIKIDATA},
        "description": {
            "value": "American electric power and natural gas holding company",
            "provenance": WIKIDATA,
        },
        "ticker": {"value": {"symbol": "DUK", "exchange": "NYSE"}, "provenance": SEC},
        "lei": {"value": "I1BZKREC126H0VB1BL91", "provenance": WIKIDATA},
        "wikipedia": {"value": "https://en.wikipedia.org/wiki/Duke_Energy", "provenance": WIKIDATA},
        "match": {"rule": "sec_cik", "confidence": 0.99, "wikidata_id": "Q1264404", "cik": "0001326160"},
    }
    base.update(overrides)
    return base


def _company(entity: dict[str, Any], *, assets: Any = None) -> FakeTransport:
    empty = (200, {"data": [], "page": {"has_more": False, "next_cursor": None}, "meta": {}})
    return _install(
        {
            "/v1/organizations": _list([entity]),
            f"/v1/organizations/{ORG_ID}": (200, {"data": entity}),
            f"/v1/organizations/{ORG_ID}/assets": assets or empty,
            f"/v1/organizations/{ORG_ID}/proposals": empty,
            f"/v1/organizations/{ORG_ID}/opportunities": empty,
            f"/v1/organizations/{ORG_ID}/nearby-proposals": empty,
        }
    )


def test_the_profile_view_keeps_only_stated_facts_each_with_its_source() -> None:
    view = organization_profile_view(
        _profile(ticker=None, headquarters={"value": {"city": None, "country": "GB"}, "provenance": WIKIDATA})
    )
    assert view is not None
    assert [(f["label"], f["text"]) for f in view["header"]] == [
        ("Website", "duke-energy.com"),
        ("Headquarters", "United Kingdom"),
    ]
    assert view["header"][0]["source"] == {
        "name": "Wikidata",
        "url": "https://www.wikidata.org/wiki/Q1264404#P856",
        "title": "Source: Wikidata (CC0). Read 22 Oct 2026.",
    }
    assert organization_profile_view(None) is None
    assert organization_profile_view(dict.fromkeys(_profile())) is None


def test_the_company_header_shows_what_it_is_and_where_each_fact_came_from(client: TestClient) -> None:
    _company(_org(website="https://www.duke-energy.com/", profile=_profile()))

    body = client.get("/organizations/duke-energy").text

    about = re.search(r'<p class="org-about" id="org-about">.*?</p>', body, re.S)
    assert about and "American electric power and natural gas holding company" in about.group(0)
    assert 'href="https://www.wikidata.org/wiki/Q1264404#P856"' in about.group(0)
    facts = re.search(r'<ul class="org-facts" id="org-facts">.*?</ul>', body, re.S)
    assert facts
    text = re.sub(r"<[^>]+>", "", facts.group(0))
    assert "Website duke-energy.com" in text and "Headquarters Charlotte, United States" in text
    assert "Listed NYSE: DUK" in text and "SEC EDGAR" in text
    assert 'title="Source: U.S. Securities and Exchange Commission, EDGAR. Read 22 Oct 2026."' in facts.group(
        0
    )
    assert (
        'id="org-fact-lei"' in body and "I1BZKREC126H0VB1BL91" in body and 'id="org-fact-wikipedia"' in body
    )
    # The website is in the header with its source, not repeated bare in the fields.
    assert "<dt>Website</dt>" not in body


def test_a_company_without_a_profile_has_no_empty_header_block(client: TestClient) -> None:
    _company(_org(website="https://curated.example/"))

    body = client.get("/organizations/duke-energy").text

    assert 'id="org-about"' not in body and 'id="org-facts"' not in body
    assert '<dt>Website</dt><dd><a href="https://curated.example/"' in body  # a curated website stays


def _asset_row(public_id: str, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "share_pct": 100.0,
        "asset": {
            "public_id": public_id,
            "slug": public_id,
            "name": public_id,
            "asset_type": "power_plant",
            "status": "operating",
        },
        "provenance": [],
    }


def test_a_figure_opens_the_assets_section_narrowed_to_what_it_counted(client: TestClient) -> None:
    rows = [_asset_row("a1", "owner"), _asset_row("a1", "operator"), _asset_row("a2", "owner")]
    totals = {"assets": 9, "by_role_and_type": {"owner": {"power_plant": 9}}}
    page = (
        200,
        {"data": rows, "page": {"has_more": False, "next_cursor": None}, "totals": totals, "meta": {}},
    )
    transport = _company(_org(), assets=page)

    body = client.get(
        "/organizations/duke-energy",
        params={"status": "operating,standby,retiring,demolished", "role": "boss"},
    ).text

    (read,) = transport.params_for(f"/v1/organizations/{ORG_ID}/assets")
    assert read["status"] == "operating,standby,retiring" and "role" not in read  # unknown values dropped
    notice = re.search(r'<p class="filter-notice" id="asset-filter-notice">(.*?)</p>', body, re.S)
    assert notice
    assert "Showing only the 2 operating, standby or retiring assets." in notice.group(1)
    # Three holdings of two assets: the headings count rows, so the line says why they add up to more.
    assert "is listed under each." in notice.group(1)
    assert 'href="/organizations/duke-energy#assets">Show every asset</a>' in notice.group(1)
    assert "Owns 9 power plants" not in body  # the whole scope's totals are not the narrowed rows


def test_the_narrowed_count_says_first_when_the_api_has_more_rows() -> None:
    notice = asset_filter_notice(
        {"status": "operating", "role": "owner"},
        [{"public_id": "a"}],
        more=True,
        clear_href="/organizations/x#assets",
    )
    assert notice == {
        "text": "Showing only operating assets it owns: the first 1 of them.",
        "clear_href": "/organizations/x#assets",
    }
    assert asset_filter_notice({}, [], more=False, clear_href="/x") is None


def test_the_page_query_is_parsed_as_the_api_documents_it() -> None:
    href = figure_href({"slug": "s"}, _queries()["pipeline_mw"], scope="group")
    assert href is not None
    assert dict(parse_qsl(urlsplit(href).query))["lifecycle_state"] == ACTIVE


# ---------------------------------------------- one asset once on the company page (fix round)
def _holding(
    public_id: str,
    role: str = "owner",
    *,
    holder: str | None = None,
    share: float | None = 100.0,
    register: str = "us.eia.860",
    asset_type: str = "power_plant",
) -> dict[str, Any]:
    """One row of `GET /v1/organizations/{id}/assets?scope=all` as the API sends it: a holding (the
    asset, the company of the group that holds it, the role, and the register that states it)."""
    row: dict[str, Any] = {
        "public_id": public_id,
        "slug": public_id,
        "name": public_id.title(),
        "asset_type": asset_type,
        "status": "operating",
        "capacity_mw": 100.0,
        "role": role,
        "share_pct": share,
        "provenance": [],
        "edge_provenance": {"source_id": register},
    }
    if holder:
        row["held_by"] = {"public_id": f"org_{holder}", "slug": holder, "name_canonical": holder.title()}
    return row


#: Vistra's shape on the 2026-10-20 store: Luminant (EIA-860) and Vistra Corp (EIA-860 and EPA GHGRP)
#: each hold an edge to the same plant, so 7 rows are 3 plants.
VISTRA_LIKE = [
    _holding("morgan-creek", holder="luminant", share=None),
    _holding("morgan-creek", holder="vistra"),
    _holding("morgan-creek", holder="vistra", register="us.epa.ghgrp"),
    _holding("sayreville", holder="vistra", register="us.epa.ghgrp"),
    _holding("sayreville", holder="vistra"),
    _holding("coleto-creek", holder="luminant", share=50.0),
    _holding("coleto-creek", holder="vistra", share=50.0),
]


def _assets_page(rows: list[dict[str, Any]], totals: dict[str, Any], *, more: bool = False) -> Any:
    return (
        200,
        {
            "data": rows,
            "page": {"has_more": more, "next_cursor": "c2" if more else None},
            "totals": totals,
            "scope": {"scope": "all", "organizations": 3},
            "meta": {},
        },
    )


def _headings(body: str) -> list[str]:
    found = re.findall(r'<h3 id="assets-[^"]+">(.*?)</h3>', body, re.S)
    return [re.sub(r"<[^>]+>", "", h).strip() for h in found]


def _summary(body: str) -> str:
    match = re.search(r'<p class="org-summary" id="org-summary">(.*?)</p>', body, re.S)
    return match.group(1) if match else ""


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.S)
    assert match, pattern
    return match.group(1) if match.groups() else match.group(0)


def test_a_narrowed_section_counts_each_asset_once_in_its_line_heading_and_rows(client: TestClient) -> None:
    """Reviewer's finding: Vistra's index figure (42) opened a section headed "Owns 48 power plants"
    under a notice that said 42; the heading and the summary counted holdings."""
    totals = {"assets": 3, "by_role_and_type": {"owner": {"power_plant": 7}}}
    _company(_org(), assets=_assets_page(VISTRA_LIKE, totals))

    body = client.get(
        "/organizations/duke-energy", params={"status": "operating,standby,retiring", "role": "owner"}
    ).text

    notice = _first(r'id="asset-filter-notice">(.*?)</p>', body)
    assert "Showing only the 3 operating, standby or retiring assets it owns." in notice
    assert "listed under each" not in notice  # one role: no asset is under two headings
    assert _headings(body) == ["Owns 3 power plants"]
    assert _summary(body) == "Owns 3 power plants"
    table = _first(r'<h3 id="assets-owner-power_plant">.*?</table>', body)
    assert table.count('<a class="row-link" href="/assets/') == 3
    # One row per plant, naming every company of the group that holds it.
    morgan = _first(r'href="/assets/morgan-creek">.*?</tr>', table)
    assert re.sub(r"<[^>]+>", "", morgan.split('data-label="Held by">')[1]).startswith("Luminant, Vistra")
    # The group's share: per register, each holder once, summed; the largest register, capped.
    assert ">100.0%<" in _first(r'href="/assets/coleto-creek">.*?</tr>', table)


def test_a_whole_section_counts_assets_not_the_holdings_the_api_table_counts(client: TestClient) -> None:
    """`totals.by_role_and_type` counts holdings (56 for Vistra's 51 plants); `by_role` and `by_type`
    count assets. With every row on the page, the line is counted over the rows."""
    totals = {
        "assets": 3,
        "by_role": {"owner": 3},
        "by_type": {"power_plant": 3},
        "by_role_and_type": {"owner": {"power_plant": 7}},
    }
    _company(_org(), assets=_assets_page(VISTRA_LIKE, totals))

    body = client.get("/organizations/duke-energy").text

    assert _summary(body) == "Owns 3 power plants"
    assert _headings(body) == ["Owns 3 power plants"]
    assert "Owns 7" not in body


def test_a_section_longer_than_one_read_takes_its_counts_from_the_api_bounded_to_assets(
    client: TestClient,
) -> None:
    totals = {
        "assets": 170,
        "by_role": {"owner": 150, "operator": 30},
        "by_type": {"power_plant": 160, "gas_pipeline": 10},
        "by_role_and_type": {
            "owner": {"power_plant": 160},
            "operator": {"gas_pipeline": 12, "power_plant": 25},
        },
    }
    _company(_org(), assets=_assets_page(VISTRA_LIKE, totals, more=True))

    body = client.get("/organizations/duke-energy").text

    assert set(_summary(body).split(" · ")) == {
        "Operates 10 gas pipelines",  # min(12 holdings, 30 operated, 10 pipelines)
        "Operates 25 power plants",
        "Owns 150 power plants",  # min(160 holdings, 150 owned): one role's exact count
    }
    # The heading is the whole count, and says the table holds the first of them.
    assert _headings(body) == ["Owns 150 power plants (the first 3 listed)"]


def test_an_asset_in_two_roles_is_under_both_headings_and_the_notice_says_why(client: TestClient) -> None:
    rows = [_holding("a1", "owner", holder="x"), _holding("a1", "operator", holder="y"), _holding("a2")]
    _company(_org(), assets=_assets_page(rows, {"assets": 2}))

    body = client.get("/organizations/duke-energy", params={"status": "operating"}).text

    notice = _first(r'id="asset-filter-notice">(.*?)</p>', body)
    assert "Showing only the 2 operating assets." in notice
    assert "An asset held in two roles is listed under each." in notice
    assert _headings(body) == ["Operates 1 power plant", "Owns 2 power plants"]


def test_the_sources_panel_credits_the_registers_behind_the_header_facts(client: TestClient) -> None:
    eia = {
        "source_id": "us.eia.860m",
        "source_name": "EIA-860M",
        "source_url": "https://www.eia.gov/electricity/data/eia860m/",
        "retrieved_at": "2026-10-09T19:51:59Z",
        "reuse_class": "open",
        "attribution_text": None,
    }
    plant = {**_holding("a1"), "provenance": [eia]}
    _company(_org(profile=_profile()), assets=_assets_page([plant], {"assets": 1}))

    body = client.get("/organizations/duke-energy").text

    panel = _first(r'<section class="provenance-panel".*?</section>', body)
    names = re.findall(r'<span class="provenance-panel__name">(.*?)</span>', panel)
    assert names == ["EIA-860M", html.escape(WIKIDATA["source_name"]), html.escape(SEC["source_name"])]
    # The credits are on the page as text, not only in a source link's tooltip.
    assert '<p class="provenance-panel__credit">Source: Wikidata (CC0)' in panel
    assert "Source: U.S. Securities and Exchange Commission, EDGAR" in panel
    # Each register links to the record read: the Wikidata item, not one property's anchor.
    assert 'href="https://www.wikidata.org/wiki/Q1264404" rel="noopener nofollow">View at source' in panel
    assert "and the facts under its name; the company record itself is derived from them." in panel


def test_a_header_fact_without_a_source_link_prints_its_source_without_a_broken_link(
    client: TestClient,
) -> None:
    unlinked = {**WIKIDATA, "source_url": None}
    hq = {"value": {"city": "Charlotte", "country": "US"}, "provenance": unlinked}
    _company(_org(profile=_profile(headquarters=hq)))

    body = client.get("/organizations/duke-energy").text

    assert 'href="None"' not in body
    item = _first(r'<li class="org-facts__item" id="org-fact-headquarters">.*?</li>', body)
    assert '<span class="fact-source" title="Source: Wikidata (CC0). Read 22 Oct 2026.">' in item
