"""The glossary (`/glossary`, web/glossary.py; Navigation v2 lane V7): one entry per term, each with what
it means, why it matters and the list that shows it.

What is pinned: every lifecycle state and notice status key in `web/labels.py` has an entry whose anchor
is the key and whose term is the chip's own word; the seven anchors record pages link to exist; every
link points at a route this site serves, with filters that route reads and values the vocabulary holds;
each status entry's "Full definition" lands on an element `/methodology/status-words` renders; and the
page renders each entry once, with its JSON-LD, and without sideways scroll at 360px
(`web/test_e2e_reference.py`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.routing import Match

from services.api.assets import ASSET_STATUSES
from services.api.slippage import SLIP_GRACE_DAYS as API_SLIP_GRACE_DAYS
from web.api_client import ApiClient
from web.app import SEARCH_FUNDING_KINDS, SEARCH_RFP_KINDS, SEARCH_TAB_KEYS
from web.app import app as web_app
from web.assets_pages import ASSET_INDEX_FILTERS
from web.glossary import (
    FUNDING_KINDS,
    GROUPS,
    RECORD_PAGE_ANCHORS,
    RFP_KINDS,
    SLIP_GRACE_DAYS,
    TERMS,
    TERMS_BY_ID,
    glossary_href,
)
from web.labels import LIFECYCLE_LABELS, OPPORTUNITY_KIND_LABELS, PROPOSAL_KIND_LABELS
from web.list_sort import (
    ASSET_COLUMNS,
    ASSET_OTHER,
    OPPORTUNITY_COLUMNS,
    OPPORTUNITY_OTHER,
    PROPOSAL_COLUMNS,
    PROPOSAL_OTHER,
    chosen_sort,
)
from web.page import ASSET_TYPE_LABELS, OPPORTUNITY_PASSTHROUGH_FILTERS, PROPOSAL_PASSTHROUGH_FILTERS
from web.viewmodels import ALL_OPPORTUNITY_STATUSES, ALL_PROPOSAL_LIFECYCLE_STATES, opportunity_status_chosen

VOCABULARY_FILE = Path(__file__).resolve().parents[1] / "data" / "vocabulary" / "lifecycle_states.yaml"

#: The six terms beyond the status words and the record pages' seven (lane brief, 2026-10-23).
MORE_TERMS = ("grid-operator", "substation", "transmission-line", "mw-and-mwh", "ppa", "cite")


def _vocabulary() -> dict[str, Any]:
    """`GET /v1/lifecycle-states` as the API builds it from the published definitions file, every state."""
    raw = yaml.safe_load(VOCABULARY_FILE.read_text(encoding="utf-8"))

    def states(section: str) -> list[dict[str, Any]]:
        return [
            {"state": key, "definition": d["definition"], "excludes": d.get("excludes"), "uncertainty": None}
            | {"is_fallback": key == "unknown", "maps_from": []}
            for key, d in raw[section].items()
        ]

    return {
        "data": {
            "definitions_written": str(raw["written"]),
            "lifecycle_states": states("lifecycle_states"),
            "opportunity_statuses": states("opportunity_statuses"),
            "pre_construction_states": [],
            "status_map_files": [],
        }
    }


COVERAGE: dict[str, Any] = {
    "data": {
        "sources": {"registered": 3, "with_rows": 1, "loaded_source_ids": [], "withheld": []},
        "vintage": {"sources": [], "oldest": None, "sources_stating_none": 0},
        "records": {"proposals": 1, "opportunities": 0, "assets": 0},
        "technologies": {"absent": []},
        "ownership": {"organizations": 1, "with_recorded_parent": 0, "without_recorded_parent": 1},
        "states": {
            "lifecycle_counts": {"filed": 1},
            "opportunity_status_counts": {},
            "pre_construction_count": 1,
            "lifecycle_absent": [],
            "opportunity_status_absent": [],
        },
        "notes": [],
    }
}


class FakeTransport:
    """Canned responses keyed by path; every call recorded. Duplicated rather than imported: this
    repo's convention is that no `web/test_*.py` imports another one."""

    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, str]] = []

    def _respond(self, method: str, url: str) -> httpx.Response:
        self.calls.append((method, url))
        status, body = self.responses.get(url, (404, {"title": "not_found", "detail": url}))
        return httpx.Response(status, json=body)

    def get(self, url: str, *, params: Any = None, cookies: Any = None) -> httpx.Response:
        return self._respond("GET", url)

    def post(self, url: str, *, json: Any = None, cookies: Any = None) -> httpx.Response:
        return self._respond("POST", url)

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return self._respond(method, url)

    def close(self) -> None:
        pass


@pytest.fixture()
def transport() -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/coverage": (200, COVERAGE),
            "/v1/lifecycle-states": (200, _vocabulary()),
        }
    )


@pytest.fixture()
def client(transport: FakeTransport) -> Iterator[TestClient]:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    web_app.state.coverage_facts = None
    with TestClient(web_app) as c:
        yield c
    web_app.state.build_info = None
    web_app.state.coverage_facts = None


# ------------------------------------------------------------------------------------- the entries
def test_every_status_key_has_an_entry_anchored_at_the_key_and_named_by_its_chip() -> None:
    for key, label in LIFECYCLE_LABELS.items():
        term = TERMS_BY_ID.get(key)
        assert term is not None, key
        assert term.term == label, key
        assert term.status, key


def test_the_record_pages_anchors_and_the_six_more_terms_exist() -> None:
    assert RECORD_PAGE_ANCHORS == (
        "interconnection-queue",
        "queue-position",
        "capacity",
        "in-service-date",
        "large-load",
        "rfp",
        "funding-call",
    )
    for anchor in (*RECORD_PAGE_ANCHORS, *MORE_TERMS):
        assert anchor in TERMS_BY_ID, anchor
        assert glossary_href(anchor) == f"/glossary#{anchor}"
    with pytest.raises(KeyError):
        glossary_href("no-such-term")


def test_ids_are_unique_url_safe_and_never_a_group_id() -> None:
    ids = [t.id for t in TERMS]
    assert len(ids) == len(set(ids))
    groups = {g.id for g in GROUPS}
    assert not groups & set(ids)
    for term_id in ids:
        assert re.fullmatch(r"[a-z][a-z0-9_-]*", term_id), term_id
    assert {t.group for t in TERMS} <= groups


def test_each_entry_is_short_and_says_what_why_and_where() -> None:
    """Precise for a professional, readable by a newcomer: two short parts and at least one list."""
    for t in TERMS:
        for part in (t.means, t.matters):
            assert part and part.strip() == part, t.id
            assert len(part.split()) <= 45, (t.id, len(part.split()))
            assert "—" not in part, t.id  # full stops and commas, not dashes
        assert t.links, t.id


def test_status_entries_fall_in_the_group_their_vocabulary_says() -> None:
    """A project status links `#state-<key>` on the status-words tab, a notice status `#notice-<key>`; the
    three words both share (unknown, announced, cancelled) sit with the projects."""
    vocabulary = yaml.safe_load(VOCABULARY_FILE.read_text(encoding="utf-8"))
    for t in TERMS:
        if not t.status:
            continue
        if t.group == "projects":
            assert t.id in vocabulary["lifecycle_states"] and t.id in ALL_PROPOSAL_LIFECYCLE_STATES, t.id
        else:
            assert t.group == "notices", t.id
            assert t.id in vocabulary["opportunity_statuses"] and t.id in ALL_OPPORTUNITY_STATUSES, t.id
            assert t.id not in ALL_PROPOSAL_LIFECYCLE_STATES, t.id


# --------------------------------------------------------------------------------------- the links
def _query_ok(path: str, query: list[tuple[str, str]]) -> None:
    names = {name for name, _ in query}
    values = dict(query)
    if path == "/proposals":
        assert names <= {*PROPOSAL_PASSTHROUGH_FILTERS, "lifecycle_state", "sort", "include_withdrawn"}
        for state in values.get("lifecycle_state", "").split(","):
            assert not state or state in ALL_PROPOSAL_LIFECYCLE_STATES, state
        if "kind" in values:
            assert values["kind"] in PROPOSAL_KIND_LABELS
        if "sort" in values:
            assert chosen_sort(values["sort"], PROPOSAL_COLUMNS, PROPOSAL_OTHER), values["sort"]
    elif path == "/opportunities":
        assert names <= {*OPPORTUNITY_PASSTHROUGH_FILTERS, "status", "sort", "relevance"}
        for status in values.get("status", "").split(","):
            assert not status or status in ALL_OPPORTUNITY_STATUSES, status
        for kind in values.get("kind", "").split(","):
            assert not kind or kind in OPPORTUNITY_KIND_LABELS, kind
        if "sort" in values:
            assert chosen_sort(values["sort"], OPPORTUNITY_COLUMNS, OPPORTUNITY_OTHER), values["sort"]
    elif path == "/assets":
        assert names <= {*ASSET_INDEX_FILTERS, "sort"}
        if "asset_type" in values:
            assert values["asset_type"] in ASSET_TYPE_LABELS
        for status in values.get("status", "").split(","):
            assert not status or status in ASSET_STATUSES, status
        if "sort" in values:
            assert chosen_sort(values["sort"], ASSET_COLUMNS, ASSET_OTHER), values["sort"]
    elif path == "/search":
        assert names <= {"q", "type"}
        assert values.get("type", "companies") in SEARCH_TAB_KEYS
    else:
        assert not names, (path, names)


def _served(path: str) -> bool:
    """Whether a GET of `path` reaches one of the site's routes (routers included), not the 404."""
    scope = {
        "type": "http",
        "path": path,
        "method": "GET",
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }
    return any(route.matches(scope)[0] == Match.FULL for route in web_app.routes)


def test_every_link_is_a_route_this_site_serves_with_filters_it_reads() -> None:
    assert not _served("/no-such-page")  # the check can fail
    for t in TERMS:
        for link in t.links:
            if link.href.startswith("#"):
                assert link.href[1:] in TERMS_BY_ID, (t.id, link.href)
                continue
            parts = urlsplit(link.href)
            assert not parts.scheme and not parts.netloc, link.href  # on this site
            assert _served(parts.path), (t.id, link.href)
            _query_ok(parts.path, parse_qsl(parts.query))


def _link(term_id: str, text: str) -> tuple[str, dict[str, str]]:
    """One entry's link by its words: its path and its query."""
    href = next(link.href for link in TERMS_BY_ID[term_id].links if link.text == text)
    parts = urlsplit(href)
    return parts.path, dict(parse_qsl(parts.query))


def _query(href: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(href).query))


def test_operating_plants_lists_operating_plants_only() -> None:
    """`/v1/assets` adds no status default (`services/api/assets.py` `_status_filter_values`), so the
    `built` entry's "Operating plants" without `status=operating` also listed the retired, retiring,
    standby and status-unknown plants (review of 2026-10-23)."""
    assert _link("built", "Operating plants") == (
        "/assets",
        {"asset_type": "power_plant", "status": "operating"},
    )


def test_the_in_service_date_links_say_what_they_show() -> None:
    """An ascending date sort over the active view leads with the stalest dates (2012 on the e2e store),
    so it is "earliest stated date first", never "soonest"; and `slipped=true` means more than
    `SLIP_GRACE_DAYS` past the date, which the entry says (review of 2026-10-23)."""
    term = TERMS_BY_ID["in-service-date"]
    assert SLIP_GRACE_DAYS == API_SLIP_GRACE_DAYS
    assert {link.text: link.href for link in term.links} == {
        "Earliest stated date first": "/proposals?sort=proposed_online_date",
        f"More than {API_SLIP_GRACE_DAYS} days past their stated date": "/proposals?slipped=true",
    }
    assert f"more than {API_SLIP_GRACE_DAYS} days" in term.matters
    assert not [link.text for t in TERMS for link in t.links if "soon" in link.text.lower()]


def test_the_rfp_and_funding_links_open_the_lists_own_default_over_every_kind() -> None:
    """A named status skips the RFPs list's "Recently closed" section (`opportunities_list` runs it only
    while no status is chosen), and on 2026-10-23 no utility RFP was open, so "Open RFPs" opened the
    empty view lane rfp-view removed; it also named one of the four kinds the entry speaks for. Each link
    names the kinds `/search` reads and no status (review of 2026-10-23)."""
    assert (RFP_KINDS, FUNDING_KINDS) == (SEARCH_RFP_KINDS, SEARCH_FUNDING_KINDS)
    for term_id, kinds in (("rfp", SEARCH_RFP_KINDS), ("funding-call", SEARCH_FUNDING_KINDS)):
        hrefs = [link.href for link in TERMS_BY_ID[term_id].links if link.href.startswith("/opportunities")]
        assert hrefs, term_id
        for href in hrefs:
            assert _query(href) == {"kind": kinds}, (term_id, href)
            assert not opportunity_status_chosen(_query(href)), href


def test_each_full_definition_lands_on_an_element_the_status_words_tab_renders(client: TestClient) -> None:
    words = client.get("/methodology/status-words").text
    glossary = client.get("/glossary").text
    links = re.findall(r'href="/methodology/status-words#([a-z_-]+)">Full definition</a>', glossary)
    assert len(links) == sum(1 for t in TERMS if t.status) == len(LIFECYCLE_LABELS)
    rendered = set(re.findall(r'id="((?:state|notice)-[a-z_]+)"', words))
    assert set(links) <= rendered, sorted(set(links) - rendered)
    # And back: every definition offers its plain-words entry.
    for anchor in rendered:
        key = anchor.split("-", 1)[1]
        assert f'<a href="/glossary#{key}">In plain words</a>' in words, anchor


# ---------------------------------------------------------------------------------------- the page
def test_the_page_renders_every_entry_once_under_its_group(client: TestClient) -> None:
    resp = client.get("/glossary")
    assert resp.status_code == 200
    body = resp.text
    assert "<h1>Glossary</h1>" in body
    for t in TERMS:
        assert body.count(f'id="{t.id}"') == 1, t.id
    for g in GROUPS:
        assert f'<h2 id="{g.id}">{g.title}</h2>' in body
        assert f'<a href="#{g.id}">{g.title}</a>' in body  # the index at the top
    entry = body.split('id="interconnection-queue"', 1)[1].split('<div class="gloss-entry"', 1)[0]
    assert "Why it matters." in entry
    assert 'href="/interconnection-points"' in entry


def test_the_page_carries_a_defined_term_set_and_is_indexed(client: TestClient) -> None:
    body = client.get("/glossary").text
    assert '<meta name="robots" content="index, follow">' in body
    graph = body.split('<script type="application/ld+json">', 1)[1].split("</script>", 1)[0]
    assert '"@type":"DefinedTermSet"' in graph
    assert graph.count('"@type":"DefinedTerm"') == len(TERMS)
    assert '"url":"http://testserver/glossary#interconnection-queue"' in graph


def test_the_page_reads_nothing_from_the_api_itself(client: TestClient, transport: FakeTransport) -> None:
    """Words and links only: the route makes no call; what is read comes from the shared header and
    footer (health, coverage), the same on every page."""
    transport.calls.clear()
    assert client.get("/glossary").status_code == 200
    assert {url for _, url in transport.calls} <= {"/v1/health", "/v1/coverage"}
