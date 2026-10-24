"""The trend series: one source per line, changes only like for like, gaps never filled, held months
and unpublishable postures left out (`services/trends/series.py`; docs/24-trends.md §10)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
from typing import Any

import pytest

from pipeline.connectors.history.documents import DOCUMENT_KIND, SCHEMA_VERSION, MonthlyStore
from services.trends import series as trends
from services.trends.store import TrendStore

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "daily-v1-2026-10-23.json"
EIA = trends.EIA
ERCOT = trends.ERCOT


def _view(
    source_id: str, credit: str = "Source: U.S. Energy Information Administration"
) -> trends.SourceView:
    return trends.SourceView(
        source_id=source_id,
        name=source_id,
        operator="Operator",
        url="https://example.org",
        licence={"licence_id": f"{source_id}#x", "reuse_class": "open"},
        attribution_text=credit,
    )


VIEWS = {EIA: _view(EIA), ERCOT: _view(ERCOT, "Source: ERCOT")}


def _tally(count: int, mw: float | None) -> dict[str, Any]:
    return {"count": count, "mw": mw, "mw_stated": count if mw is not None else 0}


def _month(
    period: str,
    mw: float,
    *,
    basis: str = "eia860m/planned/net-summer-mw/50-states-dc",
    by_technology: dict[str, float] | None = None,
    held: bool = False,
    source_id: str = EIA,
    block: str = "planned",
    parser: str = "1.0.0+aaaaaaaa",
    by_expected_year: dict[str, float] | None = None,
) -> dict[str, Any]:
    tech = by_technology if by_technology is not None else {"solar": mw}
    blocks: dict[str, Any] = {
        block: {
            "basis": basis,
            "total": _tally(10, mw),
            "by_technology": {k: _tally(1, v) for k, v in tech.items()},
            "excluded": {},
        }
    }
    if by_expected_year is not None:
        blocks[block]["by_expected_year"] = {k: _tally(1, v) for k, v in by_expected_year.items()}
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": DOCUMENT_KIND,
        "source_id": source_id,
        "period": period,
        "source_url": f"https://www.eia.gov/electricity/data/eia860m/xls/{period}.xlsx",
        "archive_url": f"https://www.eia.gov/electricity/data/eia860m/archive/xls/{period}.xlsx",
        "retrieved_at": "2026-10-23T22:00:00Z",
        "parser_version": parser,
        "blocks": blocks,
        "dq": {"status": "held" if held else "pass", "checks": []},
    }
    return doc


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    trends.clear_cache()


def _write(root: pathlib.Path, *docs: dict[str, Any]) -> None:
    store = MonthlyStore(root)
    for doc in docs:
        store.write(doc)


def _points(result: dict[str, Any], key: str = "total") -> dict[str, dict[str, Any]]:
    line = next(line for line in result["lines"] if line["key"] == key)
    return {p["period"]: p for p in line["points"]}


# ------------------------------------------------------------------------------------ monthly
def test_changes_are_computed_only_between_like_points(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        _month("2025-08", 80.0),
        _month("2025-09", 90.0),
        _month("2026-07", 1.0, basis="eia860m/planned/other-basis"),
        _month("2026-08", 100.0),
        _month("2026-09", 120.0),
    )
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw")
    points = _points(result)
    assert points["2026-09"]["mom"] == {"from": "2026-08", "value": 100.0, "change": 20.0, "pct": 20.0}
    assert points["2026-09"]["yoy"] == {"from": "2025-09", "value": 90.0, "change": 30.0, "pct": 33.33}
    assert points["2026-08"]["mom"] is None  # 2026-07 has another basis
    assert points["2026-08"]["yoy"]["from"] == "2025-08"
    assert points["2025-09"]["yoy"] is None  # no point a year before
    line = result["lines"][0]
    assert [b["period"] for b in line["breaks"]] == ["2026-07", "2026-08"]
    assert points["2026-09"]["source_url"].endswith("/xls/2026-09.xlsx")  # the newest month: its own URL
    assert "/archive/" in points["2026-08"]["source_url"]  # older months: where EIA keeps them now
    # EIA asks for the publication date: September 2026's workbook was published in October 2026
    assert result["source"]["credit"] == "Source: U.S. Energy Information Administration (Oct 2026)"
    assert result["frequency"] == "monthly" and result["first"] == "2025-08" and result["last"] == "2026-09"


def test_a_gap_is_not_filled_and_a_window_keeps_its_changes(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, _month("2026-06", 50.0), _month("2026-08", 100.0), _month("2026-09", 110.0))
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw", start="2026-09")
    points = _points(result)
    assert list(points) == ["2026-09"]
    assert points["2026-09"]["mom"]["from"] == "2026-08"  # computed from a point outside the window
    full = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw"))
    assert "2026-07" not in full and full["2026-08"]["mom"] is None


def test_a_breakdown_reads_a_missing_key_as_zero_in_a_complete_tally(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        _month("2026-08", 100.0, by_technology={"solar": 100.0}),
        _month("2026-09", 150.0, by_technology={"solar": 100.0, "storage": 50.0}),
    )
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw", by="technology")
    storage = _points(result, "storage")
    assert storage["2026-08"]["value"] == 0
    assert storage["2026-09"]["mom"]["change"] == 50.0
    assert [line["key"] for line in result["lines"]] == ["solar", "storage"]


def test_held_months_and_unseen_sources_are_left_out(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, _month("2026-08", 100.0), _month("2026-09", 500.0, held=True))
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw")
    assert list(_points(result)) == ["2026-08"]
    assert result["withheld"] == [
        {"period": "2026-09", "reason": "held by its data-quality checks", "checks": []}
    ]
    with pytest.raises(trends.SeriesError) as exc:
        trends.series(tmp_path, {ERCOT: VIEWS[ERCOT]}, "noncommercial", metric="us_planned_capacity_mw")
    assert exc.value.field == "metric"
    listed = {s["metric"] for s in trends.catalogue(tmp_path, {}, "noncommercial")}
    assert not [m for m in listed if m.startswith(("us_", "ercot_"))]
    assert "inframation_active_projects" in listed


def test_requests_name_a_series_a_breakdown_and_bounds_it_has(tmp_path: pathlib.Path) -> None:
    for kwargs, field in (
        ({"metric": "no_such_series"}, "metric"),
        ({"metric": "us_planned_capacity_mw", "by": "colour"}, "by"),
        ({"metric": "us_planned_capacity_mw", "start": "2026"}, "from"),
        ({"metric": "us_planned_capacity_mw", "end": "2026-13-01"}, "to"),
        ({"metric": "us_planned_capacity_mw", "start": "2026-09", "end": "2026-01"}, "from"),
    ):
        with pytest.raises(trends.SeriesError) as exc:
            trends.series(tmp_path, VIEWS, "noncommercial", **kwargs)  # type: ignore[arg-type]
        assert exc.value.field == field


def test_a_new_document_reaches_the_next_read(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, _month("2026-08", 100.0))
    assert trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw")["points"] == 1
    _write(tmp_path, _month("2026-09", 120.0))
    assert trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw")["points"] == 2


def test_the_catalogue_describes_each_series(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, _month("2026-09", 120.0, source_id=ERCOT, block="projects", basis="ercot-gis/x"))
    entries = {e["metric"]: e for e in trends.catalogue(tmp_path, VIEWS, "noncommercial")}
    ercot = entries["ercot_queue_mw"]
    assert ercot["frequency"] == "monthly" and ercot["first"] == ercot["last"] == "2026-09"
    assert ercot["source"]["credit"] == "Source: ERCOT"  # ERCOT asks for no date
    assert "technology" in ercot["dimensions"] and ercot["unit"] == "MW"
    assert entries["us_planned_capacity_mw"]["points"] == 0
    for entry in entries.values():
        assert entry["method"] and entry["source"]["source_id"]


# -------------------------------------------------------------------------------------- daily
def _daily(
    as_of: str, *, version: int = 2, posture: str = "noncommercial", active: int = 10
) -> dict[str, Any]:
    doc = json.loads(FIXTURE.read_text())
    doc = copy.deepcopy(doc)
    doc["as_of"] = as_of
    doc["generated_at"] = f"{as_of}T05:58:00Z"
    doc["schema_version"] = version
    doc["visibility"]["platform_posture"] = posture
    day = dt.date.fromisoformat(as_of)
    window_from = (day - dt.timedelta(days=1)).isoformat()
    doc["metrics"]["activity"]["window"] = {
        "from": f"{window_from}T00:00:00Z",
        "to_exclusive": f"{as_of}T00:00:00Z",
    }
    doc["metrics"]["proposals"]["active"]["total"]["records"] = active
    if version >= 2:
        doc["metrics"]["activity"]["rule"] = "genuine-change-v2"
        doc["metrics"]["proposals"]["withdrawn"]["by_state"] = {
            "US-TX": doc["metrics"]["proposals"]["withdrawn"]["total"]
        }
    return doc


def test_daily_series_follow_the_posture_they_were_computed_under(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_daily("2026-10-24", posture="commercial", active=10))
    store.write(_daily("2026-10-25", posture="noncommercial", active=12))
    commercial = _points(trends.series(tmp_path, VIEWS, "commercial", metric="inframation_active_projects"))
    assert list(commercial) == ["2026-10-24"]
    both = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="inframation_active_projects"))
    assert list(both) == ["2026-10-24", "2026-10-25"]
    assert both["2026-10-25"]["query"].startswith("/v1/proposals?lifecycle_state=")
    assert both["2026-10-25"]["basis"] != both["2026-10-24"]["basis"]  # postures differ: not like for like


def test_daily_changes_compare_the_same_day_a_month_before(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_daily("2026-09-25", active=8))
    store.write(_daily("2026-10-25", active=12))
    store.write(_daily("2026-10-31", active=13))
    points = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="inframation_active_projects"))
    assert points["2026-10-25"]["mom"] == {"from": "2026-09-25", "value": 8, "change": 4.0, "pct": 50.0}
    assert points["2026-10-31"]["mom"] is None  # no 31 September


def test_activity_is_dated_by_its_day_and_changes_rule_is_in_its_basis(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_daily("2026-10-23", version=1))
    store.write(_daily("2026-11-23", version=2))
    points = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="inframation_changes"))
    assert list(points) == ["2026-10-22", "2026-11-22"]
    assert "/genuine-change-v1/" in points["2026-10-22"]["basis"]
    assert points["2026-11-22"]["mom"] is None  # v1 and v2 rules count differently


def test_a_breakdown_a_schema_did_not_record_is_absent_not_zero(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_daily("2026-10-23", version=1))
    store.write(_daily("2026-10-24", version=2))
    result = trends.series(
        tmp_path, VIEWS, "noncommercial", metric="inframation_withdrawn_projects", by="state"
    )
    tx = _points(result, "US-TX")
    assert "2026-10-23" not in tx  # no point at all, neither a zero nor a null
    withdrawn = json.loads(FIXTURE.read_text())["metrics"]["proposals"]["withdrawn"]["total"]["records"]
    assert tx["2026-10-24"]["value"] == withdrawn


# --------------------------------------------------------------------------- like for like, more
def test_months_read_by_two_parsers_never_compare(tmp_path: pathlib.Path) -> None:
    """A month whose restatement failed stays under its old parser: it keeps its value, but no change
    is computed against it, and the line lists the break with both parsers."""
    _write(
        tmp_path,
        _month("2026-07", 90.0),
        _month("2026-08", 100.0, parser="0.9.0+old"),
        _month("2026-09", 120.0),
    )
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw")
    points = _points(result)
    assert points["2026-08"]["value"] == 100.0
    assert points["2026-08"]["mom"] is None and points["2026-09"]["mom"] is None
    [first, second] = result["lines"][0]["breaks"]
    assert first["period"] == "2026-08" and first["basis_before"] == first["basis_after"]
    assert (first["parser_before"], first["parser_after"]) == ("1.0.0+aaaaaaaa", "0.9.0+old")
    assert second["period"] == "2026-09"
    assert "_like" not in points["2026-09"] and "_parser" not in points["2026-09"]


def test_a_years_line_of_a_stock_computes_no_change_from_that_year_on(tmp_path: pathlib.Path) -> None:
    """EIA's planned capacity by expected year: the 2026 line empties through 2026 as its plants come
    online (yoy -47.7 % in 2026-09 on the full archive). That is completion, not a shrinking pipeline:
    no change is computed on a year's line from the start of the year, while later years' lines keep
    theirs."""
    _write(
        tmp_path,
        _month("2025-09", 300.0, by_expected_year={"2026": 200.0, "2027": 100.0}),
        _month("2026-08", 260.0, by_expected_year={"2026": 120.0, "2027": 140.0}),
        _month("2026-09", 250.0, by_expected_year={"2026": 105.0, "2027": 145.0}),
    )
    result = trends.series(
        tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw", by="expected_year"
    )
    y2026, y2027 = _points(result, "2026"), _points(result, "2027")
    assert y2026["2026-09"]["value"] == 105.0
    assert y2026["2026-09"]["yoy"] is None and y2026["2026-09"]["mom"] is None
    assert y2027["2026-09"]["yoy"]["change"] == 45.0 and y2027["2026-09"]["mom"]["change"] == 5.0
    assert "stocks" in result["method"]


def test_a_newer_reading_held_beside_a_month_is_listed_and_the_month_still_served(
    tmp_path: pathlib.Path,
) -> None:
    _write(tmp_path, _month("2026-08", 100.0), _month("2026-09", 120.0))
    pending = _month("2026-08", 120.0, held=True)
    pending["file_id"] = "1295000000"
    pending["dq"]["checks"] = [
        {"check": "period_mismatch", "level": "hold"},
        {"check": "duplicate_file", "level": "hold"},
    ]
    MonthlyStore(tmp_path).write_pending(pending)
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw")
    points = _points(result)
    assert points["2026-08"]["value"] == 100.0  # the month's earlier document, not the held reading
    assert points["2026-09"]["mom"]["change"] == 20.0
    [held] = result["held_readings"]
    assert held["period"] == "2026-08" and held["checks"] == ["duplicate_file", "period_mismatch"]
    assert held["file_id"] == "1295000000" and result["withheld"] == []


def test_a_month_that_did_not_tally_a_dimension_has_no_point_on_its_lines(tmp_path: pathlib.Path) -> None:
    older = _month("2026-08", 100.0)
    del older["blocks"]["planned"]["by_technology"]
    _write(tmp_path, older, _month("2026-09", 120.0))
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw", by="technology")
    assert list(_points(result, "solar")) == ["2026-09"]


def _with_sources(doc: dict[str, Any], *sources: tuple[str, str | None], withheld: int = 2) -> dict[str, Any]:
    doc["sources"] = [
        {
            "source_id": sid,
            "name": f"Register {sid}",
            "operator": "Operator",
            "licence_id": f"{sid}#lic",
            "licence_name": "Licence",
            "licence_url": None,
            "reuse_class": "attribution",
            "attribution_text": credit,
            "requires_link_back": False,
            "proposal_links": 5,
            "opportunity_links": 0,
            "counted_events": 0,
            "retrieved_at_max": f"{doc['as_of']}T04:00:00Z",
        }
        for sid, credit in sources
    ]
    doc["visibility"]["withheld_sources"] = withheld
    return doc


CAISO = ("us.iso.caiso.gen_queue", "Source: California ISO")
MISO = ("us.iso.miso.gen_queue", None)


def test_a_source_going_live_is_a_break_not_growth(tmp_path: pathlib.Path) -> None:
    """A new register connector goes live (or MISO's terms are recorded and its rows published): the
    active count jumps without anything changing in the world. The coverage is in the basis, so that
    day is a break, never a month-on-month change."""
    store = TrendStore(tmp_path)
    store.write(_with_sources(_daily("2026-10-24", active=10), CAISO))
    store.write(_with_sources(_daily("2026-11-24", active=30), CAISO, MISO, withheld=1))
    store.write(_with_sources(_daily("2026-12-24", active=33), CAISO, MISO, withheld=1))
    result = trends.series(tmp_path, VIEWS, "noncommercial", metric="inframation_active_projects")
    points = _points(result)
    assert points["2026-11-24"]["mom"] is None  # not +200 %: MISO joined
    assert points["2026-12-24"]["mom"]["change"] == 3.0  # the same sources both days: like for like
    [brk] = result["lines"][0]["breaks"]
    assert brk["period"] == "2026-11-24"
    assert brk["sources_added"] == ["us.iso.miso.gen_queue"] and brk["sources_removed"] == []
    assert (brk["withheld_before"], brk["withheld_after"]) == (2, 1)


def test_a_source_withheld_or_released_alone_is_also_a_break(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_with_sources(_daily("2026-10-24", active=10), CAISO, withheld=2))
    store.write(_with_sources(_daily("2026-11-24", active=10), CAISO, withheld=3))
    points = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="inframation_active_projects"))
    assert points["2026-11-24"]["mom"] is None


def test_the_changes_figure_names_its_document_and_no_list_query(tmp_path: pathlib.Path) -> None:
    """`activity.visible_events` lists the left-out events too, so it is not this figure's list."""
    TrendStore(tmp_path).write(_daily("2026-10-24"))
    changes = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="inframation_changes"))
    [point] = changes.values()
    assert point["query"] is None and point["document"] == "2026-10-24"
    active = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="inframation_active_projects"))
    assert active["2026-10-24"]["query"].startswith("/v1/proposals?")


def test_every_contributing_source_of_a_served_day_is_credited(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_with_sources(_daily("2026-10-24"), CAISO))
    store.write(_with_sources(_daily("2026-10-25"), ("us.iso.nyiso.gen_queue", "Source: NYISO")))
    rows = trends.daily_source_rows(tmp_path, "noncommercial", {"2026-10-25"})
    assert [(r["source_id"], r["attribution_text"], r["document"]) for r in rows] == [
        ("us.iso.nyiso.gen_queue", "Source: NYISO", "2026-10-25")
    ]
    assert len(trends.daily_source_rows(tmp_path, "noncommercial")) == 2
    entries = {e["metric"]: e for e in trends.catalogue(tmp_path, {}, "noncommercial")}
    assert (
        entries["inframation_active_projects"]["source"]["credit"] == "Source: California ISO; Source: NYISO"
    )


def test_a_month_its_checks_warned_on_carries_the_warning_on_its_points(tmp_path: pathlib.Path) -> None:
    """EIA's 2026-08 Retired list added 25 old units (9.4 GW) that 2026-09 took out again: both months
    are served, and their points name the warning, so a chart can flag the change as EIA's restatement.
    Another block's series is not flagged by it."""
    before = _month("2026-07", 264_000.0, block="retired", basis="eia860m/retired/x/retired-since-2002")
    restated = _month("2026-08", 273_684.8, block="retired", basis="eia860m/retired/x/retired-since-2002")
    restated["dq"] = {
        "status": "warn",
        "checks": [{"check": "cumulative_restated", "block": "retired", "level": "warn"}],
    }
    planned = _month("2026-08", 100.0)
    restated["blocks"].update(planned["blocks"])
    _write(tmp_path, before, restated)
    retired = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="us_retired_capacity_mw"))
    assert retired["2026-08"]["dq_warnings"] == ["cumulative_restated"]
    assert "dq_warnings" not in retired["2026-07"]
    planned_points = _points(trends.series(tmp_path, VIEWS, "noncommercial", metric="us_planned_capacity_mw"))
    assert "dq_warnings" not in planned_points["2026-08"]
