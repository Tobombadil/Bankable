"""Incremental fetch windows are anchored to the last promoted run (audit 2026-09-30 data engineer
F3, F12): a gap is caught up, a held run leaves the watermark where it was, a fetch that stops at
its page cap is held, and a run right after the last one asks only for what is new.

Every fetch here goes to an in-process fake of the upstream API; nothing touches the network.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

from conftest import FIXTURES, connector_for
from pipeline.connectors import eu_ted_api
from pipeline.connectors.base import FetchWindow
from pipeline.connectors.eu_ted_api import connector as ted
from pipeline.connectors.gb_find_a_tender import connector as fts
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store

assert eu_ted_api  # the package import keeps the module path explicit for monkeypatching


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def _business_days(first: dt.date, last: dt.date) -> list[dt.date]:
    out, d = [], first
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


class FakeTed:
    """TED's search endpoint: filters on `publication-date>=YYYYMMDD`, pages by `limit`/`page`."""

    def __init__(self, per_day: int = 3, first=dt.date(2026, 9, 1), last=dt.date(2026, 9, 30)) -> None:
        template = json.loads((FIXTURES / "ted_search.json").read_text())["pages"][0]["notices"][0]
        template.pop("links", None)
        self.notices: list[dict[str, Any]] = []
        for day in _business_days(first, last):
            for k in range(per_day):
                n = copy.deepcopy(template)
                n["publication-number"] = f"{day:%m%d}{k:02d}-2026"
                n["publication-date"] = f"{day.isoformat()}+02:00"
                self.notices.append(n)
        self.calls: list[dict[str, Any]] = []

    def post(
        self, url: str, json: dict[str, Any] | None = None, timeout: float | None = None
    ) -> FakeResponse:
        assert json is not None
        self.calls.append(json)
        since = re.search(r"publication-date>=(\d{8})", json["query"])
        assert since, json["query"]
        sel = [n for n in self.notices if n["publication-date"][:10].replace("-", "") >= since.group(1)]
        page, limit = int(json["page"]), int(json["limit"])
        return FakeResponse({"notices": sel[(page - 1) * limit : page * limit], "totalNoticeCount": len(sel)})

    def since(self, call: int = -1) -> str:
        m = re.search(r"publication-date>=(\d{8})", self.calls[call]["query"])
        assert m
        return m.group(1)


def _published(df: pd.DataFrame) -> set[str]:
    return set(df["source_record_id"])


T1 = dt.datetime(2026, 9, 13, 20, 26, tzinfo=dt.UTC)  # the last run before the outage the audit found
T2 = dt.datetime(2026, 9, 30, 3, 7, tzinfo=dt.UTC)  # the first daily tick after it


def test_the_first_run_after_a_gap_catches_up(tmp_path):
    api = FakeTed()
    st = Store(tmp_path)
    r1 = run("eu.ted.api", store=st, http=api, now=T1, trigger="schedule")
    assert r1.status == "ok"
    assert api.since() == "20260910"  # a first run has nothing to anchor on: the rolling 3 days

    r2 = run("eu.ted.api", store=st, http=api, now=T2, trigger="schedule")
    assert r2.status == "ok", r2.run.get("hold_reasons")
    # Anchored on the last promoted run (09-13) minus a day's overlap, not on "now - 3 days" (09-27).
    assert api.since() == "20260912"
    gap = {
        n["publication-number"]
        for n in api.notices
        if "2026-09-14" <= n["publication-date"][:10] <= "2026-09-29"
    }
    assert len(gap) == 36  # 12 business days x 3
    assert gap <= _published(r2.records)
    meta = r2.run["snapshot"]["meta"]
    assert meta["window_anchor"] == "watermark"
    assert meta["watermark"] == "2026-09-13T20:26:00+00:00"
    assert any(c["check"] == "fetch_window" for c in r2.run["dq"]["checks"])


def test_a_held_run_leaves_the_watermark_where_it_was(tmp_path, monkeypatch):
    api = FakeTed()
    st = Store(tmp_path)
    assert run("eu.ted.api", store=st, http=api, now=T1).status == "ok"

    # A page cap the catch-up cannot fit under: 2 notices a page, 1 page normally (x6 for the gap).
    monkeypatch.setattr(ted, "PAGE", 2)
    monkeypatch.setattr(ted, "MAX_PAGES", 1)
    held = run("eu.ted.api", store=st, http=api, now=T2)
    assert held.status == "partial"
    assert held.run["snapshot"]["meta"]["truncated"] is True
    assert any(r.startswith("window_truncated") for r in held.run["hold_reasons"])

    monkeypatch.setattr(ted, "PAGE", 100)
    monkeypatch.setattr(ted, "MAX_PAGES", 30)
    later = run("eu.ted.api", store=st, http=api, now=T2 + dt.timedelta(days=1))
    assert later.status == "ok"
    assert api.since() == "20260912", "the held run was not promoted, so the window still starts at 09-12"


def test_fetch_window_rules(registry):
    c = connector_for("eu.ted.api", registry)
    now = dt.datetime(2026, 10, 7, 3, 7, tzinfo=dt.UTC)
    assert c.fetch_window(now) == FetchWindow(now - dt.timedelta(days=3), now, "rolling")

    c.watermark = dt.datetime(2026, 10, 6, 3, 7, tzinfo=dt.UTC)
    w = c.fetch_window(now)
    assert (w.start, w.anchor) == (dt.datetime(2026, 10, 5, tzinfo=dt.UTC), "watermark")

    c.watermark = now - dt.timedelta(days=200)  # longer than max_catchup_days: clamped and said so
    w = c.fetch_window(now)
    assert w.anchor == "clamped" and w.start == now - dt.timedelta(days=c.max_catchup_days)


def test_a_clamped_window_warns(tmp_path):
    api = FakeTed(first=dt.date(2026, 3, 2), last=dt.date(2026, 3, 13))
    st = Store(tmp_path)
    assert (
        run("eu.ted.api", store=st, http=api, now=dt.datetime(2026, 3, 13, 4, tzinfo=dt.UTC)).status == "ok"
    )
    late = run("eu.ted.api", store=st, http=api, now=dt.datetime(2026, 9, 30, 4, tzinfo=dt.UTC))
    check = next(c for c in late.run["dq"]["checks"] if c["check"] == "fetch_window")
    assert check["level"] == "warn" and "clamped" in check["detail"]
    assert late.run["dq_status"] in ("warn", "fail")


# ------------------------------------------------------------------------- F12: request counts
class FakeFts:
    """Find a Tender's release packages: `updatedFrom` filter, 100 a page, `links.next` cursor."""

    def __init__(self, per_day: int, first: dt.datetime, days: int) -> None:
        step = dt.timedelta(days=1) / per_day
        self.releases = [
            {
                "ocid": f"ocds-x-{i:06d}",
                "id": f"{i:06d}-2026",
                "tag": ["tender"],
                "date": (first + i * step).isoformat(),
            }
            for i in range(per_day * days)
        ]
        self.requests = 0
        self.now = first + dt.timedelta(days=days)

    def get(self, url: str, honour_robots: bool = True, timeout: float | None = None) -> FakeResponse:
        self.requests += 1
        q = parse_qs(urlsplit(url).query)
        since = dt.datetime.fromisoformat(q["updatedFrom"][0]).replace(tzinfo=dt.UTC)
        cursor = int(q.get("cursor", ["0"])[0])
        sel = [r for r in self.releases if since <= dt.datetime.fromisoformat(r["date"]) < self.now]
        chunk = sel[cursor * 100 : (cursor + 1) * 100]
        nxt = f"{url.split('&cursor=')[0]}&cursor={cursor + 1}" if (cursor + 1) * 100 < len(sel) else None
        return FakeResponse({"releases": chunk, "links": {"next": nxt} if nxt else {}})


@pytest.mark.parametrize(
    ("label", "every", "anchored", "expected_max"),
    [
        ("before: 15-minute floor, rolling 2 days", 15, False, None),
        ("after: hourly, anchored", 60, True, 130),
    ],
)
def test_find_a_tender_requests_per_day(registry, label, every, anchored, expected_max):
    """~300 releases a day (6 pages per rolling 2-day window, the 2026-09-12 manifest note): the
    15-minute rolling poll costs 576 requests a day, the hourly anchored poll about a fifth."""
    day = dt.datetime(2026, 10, 7, tzinfo=dt.UTC)
    api = FakeFts(per_day=300, first=day - dt.timedelta(days=5), days=6)
    c = connector_for("gb.find_a_tender", registry, http=api)
    last_ok: dt.datetime | None = day - dt.timedelta(minutes=every)
    t = day
    while t < day + dt.timedelta(days=1):
        c.clock = lambda t=t: t
        api.now = t
        c.watermark = last_ok if anchored else None
        c.fetch()
        last_ok = t
        t += dt.timedelta(minutes=every)
    if expected_max is None:
        assert api.requests == 96 * 6 == 576, label
    else:
        assert api.requests <= expected_max, (label, api.requests)


def test_an_hourly_anchored_window_covers_the_previous_complete_day(registry):
    c = connector_for("gb.find_a_tender", registry)
    now = dt.datetime(2026, 10, 7, 9, 11, tzinfo=dt.UTC)
    c.watermark = now - dt.timedelta(hours=1)
    w = c.fetch_window(now)
    assert w.start == dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
    assert fts.WINDOW_DAYS == c.window_days == 2
