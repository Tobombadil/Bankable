"""Freshness claims follow the sources (expert review 2026-10-07, interconnection finding 9; large-load
finding 8).

Before: the masthead said "Every record and every change live" and the tier notice stamped "Live" on
every page, and the footer's "fetched 2026-10-07" was the newest fetch (EIA-860M), while CAISO, NYISO
and NESO were last fetched 2026-09-13, three times past their weekly allowance. The claims are now
derived from `/v1/coverage`'s per-source `fetched_at` and `freshness`, and say nothing about being
live when the API cannot tell.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import summarise_freshness


def _row(source_id: str, status: str, fetched: str | None) -> dict[str, Any]:
    return {"source_id": source_id, "name": source_id, "fetched_at": fetched, "freshness": {"status": status}}


ROWS = [
    _row("us.eia.860m", "fresh", "2026-10-07T10:36:13"),
    _row("us.iso.caiso.gen_queue", "stale", "2026-09-13T20:25:21"),
    _row("gb.neso.tec_register", "stale", "2026-09-13T20:26:18"),
    _row("gb.find_a_tender", "never", None),
    _row("us.eia.atlas.gas_pipelines", "unscheduled", "2026-09-19T15:34:29"),
]


def test_summary_counts_scheduled_sources_and_names_the_late_ones() -> None:
    summary = summarise_freshness(ROWS)
    assert summary["available"] and summary["scheduled"] == 4  # the unpolled atlas file is not counted
    assert [b["source_id"] for b in summary["behind"]] == [
        "gb.find_a_tender",
        "us.iso.caiso.gen_queue",
        "gb.neso.tec_register",
    ]
    assert summary["oldest_fetch"].startswith("2026-09-13") and summary["newest_fetch"].startswith(
        "2026-10-07"
    )
    assert summarise_freshness([_row("x", "unscheduled", None)]) == {
        "available": False,
        "scheduled": 0,
        "behind": [],
        "oldest_fetch": None,
        "newest_fetch": None,
    }


LAG = {"supply": 0, "opportunities": 0}


class FakeTransport:
    def __init__(self, coverage_rows: list[dict[str, Any]] | None) -> None:
        self.coverage_rows = coverage_rows

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        if url == "/v1/coverage":
            if self.coverage_rows is None:
                return httpx.Response(503, json={"title": "unavailable"})
            return httpx.Response(200, json={"data": {"vintage": {"sources": self.coverage_rows}}})
        if url == "/v1/health":
            return httpx.Response(
                200, json={"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}
            )
        if url == "/v1/meta/vocabularies":
            vocab = {k: [] for k in ("technology", "proposal_kind", "opportunity_kind", "opportunity_status")}
            return httpx.Response(200, json={"data": {**vocab, "slip_bucket": []}})
        empty = {
            "data": [],
            "meta": {"total": 0},
            "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
        }
        return httpx.Response(200, json=empty)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={})

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={})

    def close(self) -> None:
        pass


def _forget(key: str) -> None:
    """`State` keeps attributes in its own dict, so `del` is the way to drop one."""
    try:
        delattr(web_app.state, key)
    except (AttributeError, KeyError):
        pass


@pytest.fixture()
def install() -> Iterator[Any]:
    def _install(rows: list[dict[str, Any]] | None) -> None:
        web_app.state.api_client = ApiClient(FakeTransport(rows))
        web_app.state.lag_days_default = None
        _forget("coverage_data")

    yield _install
    for key in ("api_client", "lag_days_default", "coverage_data", "build_info"):
        _forget(key)


def _text(html: str, cls: str) -> str:
    end = "div" if cls == "delayed-notice" else "span"
    match = re.search(rf'class="{cls}"[^>]*>(.*?)</{end}>', html, re.S)
    assert match, cls
    return re.sub(r"<[^>]+>|\s+", " ", match.group(1)).strip()


def test_late_sources_turn_live_into_no_delay_and_are_named(install: Any) -> None:
    install(ROWS)
    with TestClient(web_app) as client:
        html = client.get("/proposals").text
    assert "Every record and every change live" not in html
    assert _text(html, "masthead__lag") == "3 of 4 sources behind their fetch schedule"
    notice = _text(html, "delayed-notice")
    assert notice.startswith("No delay ") and "3 sources are behind their fetch schedule" in notice
    assert "CAISO (last fetched 13 Sep 2026)" in notice and "NESO" in notice
    assert "Find a Tender (never fetched)" in notice
    assert "sources last fetched between" in html and "13 Sep 2026" in html


def test_all_on_schedule_may_say_live(install: Any) -> None:
    install([ROWS[0], ROWS[4]])
    with TestClient(web_app) as client:
        html = client.get("/proposals").text
    assert _text(html, "masthead__lag") == "All 1 scheduled sources fetched on schedule"
    assert _text(html, "delayed-notice").startswith("Live ")


def test_no_claim_when_the_api_cannot_say(install: Any) -> None:
    install(None)
    with TestClient(web_app) as client:
        html = client.get("/proposals").text
    assert _text(html, "masthead__lag") == "Source fetch dates"
    assert _text(html, "delayed-notice").startswith("No delay ")
