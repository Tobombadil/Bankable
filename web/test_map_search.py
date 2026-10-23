"""The home map's search field and the panel's busiest areas (`web/map_search.py`; owner, 2026-10-22;
docs/31 §5.18, §5.19). Server halves only, over a fake API; the browser halves are in
`web/test_e2e.py`. The fake transport is duplicated from `web/test_layout_states.py`, per this repo's
convention that no `web/test_*.py` imports another."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web import map_search
from web.api_client import ApiClient
from web.app import app as web_app
from web.map_search import (
    AREA_CACHE_SECONDS,
    AREAS,
    PLACES,
    bbox_of,
    busiest_areas,
    county_label,
    match_places,
    words_of,
)

Answer = tuple[int, Any] | Callable[[Mapping[str, Any]], tuple[int, Any]]


class FakeTransport:
    def __init__(self, responses: Mapping[str, Answer]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        if url in self.responses:
            answer = self.responses[url]
            status, body = answer(params or {}) if callable(answer) else answer
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def close(self) -> None:
        pass

    def params_for(self, url: str) -> list[dict[str, Any]]:
        return [params for called, params in self.calls if called == url]


def _square(west: float, south: float, east: float, north: float) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }


#: Two made-up states side by side and one county in the first, as `GET /v1/geo/regions` serves them.
STATES = [
    {
        "type": "Feature",
        "properties": {"region_id": "US-AA", "name": "Alpha"},
        "geometry": _square(-100, 30, -95, 35),
    },
    {
        "type": "Feature",
        "properties": {"region_id": "US-BB", "name": "Beta"},
        "geometry": _square(-95, 30, -90, 35),
    },
]
COUNTIES = [
    {
        "type": "Feature",
        "properties": {"region_id": "01001", "name": "Travis", "state_code": "US-AA"},
        "geometry": _square(-98, 31, -97, 32),
    }
]
COUNTRIES = [
    {
        "type": "Feature",
        "properties": {"region_id": "GB", "name": "United Kingdom"},
        "geometry": _square(-8, 50, 2, 59),
    }
]


def _regions(params: Mapping[str, Any]) -> tuple[int, Any]:
    features = {"state": STATES, "county": COUNTIES, "country": COUNTRIES}.get(str(params.get("level")), [])
    return 200, {"data": {"type": "FeatureCollection", "features": features}}


def _cluster(lon: float, lat: float, count: int, mw: float) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"feature_kind": "cluster", "count": count, "capacity_mw_sum": mw},
    }


def _geo(params: Mapping[str, Any]) -> tuple[int, Any]:
    """Three cells in Alpha (5 + 1 projects), one in Beta (4), one in GB (2), one on open sea (1),
    a point in Beta, and two region-placed groups: a county of Alpha (3) and Beta itself (2)."""
    features = [
        _cluster(-97.5, 31.5, 5, 500.0),
        _cluster(-99.0, 33.0, 1, 50.0),
        _cluster(-92.0, 32.0, 4, 1200.0),
        _cluster(-1.0, 52.0, 2, 30.0),
        _cluster(-60.0, 20.0, 1, 10.0),
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-91.0, 31.0]},
            "properties": {"feature_kind": "proposal", "capacity_mw": 100.0},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-97.5, 31.5]},
            "properties": {
                "feature_kind": "region",
                "region_level": "county",
                "region_id": "01001",
                "count": 3,
                "capacity_mw_sum": 90.0,
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-92.5, 32.5]},
            "properties": {
                "feature_kind": "region",
                "region_level": "state",
                "region_id": "US-BB",
                "count": 2,
                "capacity_mw_sum": 0,
            },
        },
    ]
    return 200, {"data": {"type": "FeatureCollection", "features": features, "totals": {"records": 99}}}


@pytest.fixture()
def fake() -> Iterator[FakeTransport]:
    transport = FakeTransport({"/v1/geo/regions": _regions, "/v1/proposals/geo": _geo})
    PLACES.reset()
    AREAS.reset()
    map_search.reset_area_cache()
    yield transport
    PLACES.reset()
    AREAS.reset()
    map_search.reset_area_cache()


def test_busiest_areas_counts_cells_by_the_outline_that_holds_them_and_regions_by_their_own_id(
    fake: FakeTransport,
) -> None:
    api = ApiClient(fake)
    body = busiest_areas(api, {"bbox": "-179,-85,179,85", "lifecycle_state": "filed", "zoom": "3"})
    # Alpha: 5 + 1 cells and its county's 3; Beta: 4 + the point + its own 2; GB 2; the sea cell
    # counts to the total and to no area.
    assert [(a["id"], a["label"], a["count"], a["capacity_mw"]) for a in body["areas"]] == [
        ("US-AA", "Alpha", 9, 640.0),
        ("US-BB", "Beta", 7, 1300.0),
        ("GB", "United Kingdom", 2, 30.0),
    ]
    assert body["total"] == 19
    assert body["areas"][0]["bbox"] == [-100.0, 30.0, -95.0, 35.0]  # where "Alpha" flies to
    # One read at the API's finest grid, with the caller's filters and bounds, never its zoom.
    (asked,) = fake.params_for("/v1/proposals/geo")
    assert asked == {"bbox": "-179,-85,179,85", "lifecycle_state": "filed", "zoom": "12"}


def test_busiest_areas_are_kept_for_a_while_per_query(fake: FakeTransport) -> None:
    api = ApiClient(fake)
    first = busiest_areas(api, {"bbox": "1,2,3,4"}, now=1000.0)
    assert busiest_areas(api, {"bbox": "1,2,3,4"}, now=1000.0 + AREA_CACHE_SECONDS - 1) is first
    assert len(fake.params_for("/v1/proposals/geo")) == 1
    busiest_areas(api, {"bbox": "1,2,3,5"}, now=1001.0)  # another view: asked
    busiest_areas(api, {"bbox": "1,2,3,4"}, now=1000.0 + AREA_CACHE_SECONDS + 1)  # stale: asked again
    assert len(fake.params_for("/v1/proposals/geo")) == 3


def test_the_areas_route_answers_no_status_without_asking_the_api(fake: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(fake)
    try:
        with TestClient(web_app) as client:
            none = client.get("/api/map/areas", params={"lifecycle_state": "none", "bbox": "-179,-85,179,85"})
            some = client.get("/api/map/areas", params={"bbox": "-179,-85,179,85", "kind": "generation"})
    finally:
        web_app.state.__dict__.pop("api_client", None)
    assert none.json() == {"areas": [], "total": 0}
    assert [a["id"] for a in some.json()["areas"]] == ["US-AA", "US-BB", "GB"]
    (asked,) = fake.params_for("/v1/proposals/geo")
    assert asked["kind"] == "generation" and asked["zoom"] == "12" and asked["listed"] == "true"


def test_place_words_labels_and_matching(fake: FakeTransport) -> None:
    assert words_of("St. Louis") == ["st", "louis"] and words_of("Doña Ana") == ["dona", "ana"]
    assert county_label("48453", "Travis", "TX", "Texas") == "Travis County, TX"
    assert county_label("22071", "Orleans", "LA", "Louisiana") == "Orleans Parish, LA"
    assert county_label("24510", "Baltimore", "MD", "Maryland") == "Baltimore (city), MD"
    # Aleutians cross the antimeridian: the box is Alaska's western-hemisphere part.
    assert bbox_of(
        {"type": "Polygon", "coordinates": [[[-179.5, 51], [179.5, 52], [-130, 60], [-179.5, 51]]]}
    ) == (
        -179.5,
        51.0,
        -130.0,
        60.0,
    )
    places = PLACES.places(ApiClient(fake))
    assert [p.label for p in match_places(places, "trav")] == ["Travis County, AA"]
    assert [p.label for p in match_places(places, "travis alpha")] == ["Travis County, AA"]
    assert [p.label for p in match_places(places, "alp")] == ["Alpha"]  # a state before a county
    assert match_places(places, "zz") == []


def test_a_query_under_two_characters_asks_nothing(fake: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(fake)
    try:
        with TestClient(web_app) as client:
            body = client.get("/api/map/search", params={"q": " t "}).json()
    finally:
        web_app.state.__dict__.pop("api_client", None)
    assert body == {"q": "t", "places": [], "companies": [], "projects": []}
    assert fake.calls == []
