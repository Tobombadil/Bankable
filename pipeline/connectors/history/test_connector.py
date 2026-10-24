"""The history family's shared parts: the monthly documents, the data-quality checks, the runner's
modes and failure ledger, the gate and the CLI. The connectors' own tests are in `eia860m/` and
`ercot_gis/`."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from pipeline.connectors.history import HISTORY_CONNECTORS, connector_class_for
from pipeline.connectors.history import __main__ as cli
from pipeline.connectors.history.base import (
    Block,
    HistoryGateRefused,
    Tally,
    find_header,
    gate,
    number,
    year_of,
)
from pipeline.connectors.history.documents import (
    DOCUMENT_KIND,
    SCHEMA_VERSION,
    DocumentError,
    MonthlyStore,
    document_bytes,
    is_held,
    month_number,
    shift_period,
)
from pipeline.connectors.history.dq import RESTATED_SHARE, check_month
from pipeline.connectors.history.runner import consecutive_failures, effective_mode, run
from pipeline.connectors.registry import SourceEntry


def _doc(period: str = "2026-09", **blocks: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DOCUMENT_KIND,
        "source_id": "us.eia.860m.history",
        "period": period,
        "blocks": blocks
        or {"operating": {"basis": "b", "total": {"count": 100, "mw": 1.0, "mw_stated": 100}}},
    }


# ---------------------------------------------------------------------------------- documents
def test_periods_and_month_names() -> None:
    assert shift_period("2026-01", -1) == "2025-12"
    assert shift_period("2025-12", 13) == "2027-01"
    assert month_number("Sep") == 9 and month_number("sept.") == 9 and month_number("June") == 6
    assert month_number("Ju") is None and month_number("Smarch") is None
    with pytest.raises(ValueError):
        shift_period("2026-13", 1)


def test_a_document_round_trips_and_a_newer_schema_is_refused(tmp_path) -> None:
    store = MonthlyStore(tmp_path)
    path = store.write(_doc())
    assert path.name == "2026-09.json"
    assert store.load("us.eia.860m.history", "2026-09")["period"] == "2026-09"
    assert store.periods("us.eia.860m.history") == ["2026-09"]
    assert document_bytes(_doc()).endswith(b"}\n")
    newer = {**_doc("2026-10"), "schema_version": SCHEMA_VERSION + 1}
    with pytest.raises(DocumentError):
        store.write(newer)
    path.with_name("2026-10.json").write_text(json.dumps(newer))
    # an unreadable or newer file is skipped by the reader, never misread
    assert [d["period"] for d in store.documents("us.eia.860m.history")] == ["2026-09"]
    with pytest.raises(ValueError):
        store.dir_for("../escape")


def test_a_held_reading_waits_beside_a_served_month_until_released(tmp_path) -> None:
    """A held reading of a month that has a served document is kept beside it, never over it:
    `periods` and `documents` still give the served one; `release` makes the held one the month."""
    store = MonthlyStore(tmp_path)
    store.write({**_doc(), "file_id": "good", "dq": {"status": "pass", "checks": []}})
    with pytest.raises(DocumentError):
        store.write_pending({**_doc(), "dq": {"status": "pass"}})  # only a held reading waits beside
    store.write_pending({**_doc(), "file_id": "newer", "dq": {"status": "held", "checks": []}})
    assert store.periods("us.eia.860m.history") == ["2026-09"]
    assert [d["file_id"] for d in store.documents("us.eia.860m.history")] == ["good"]
    assert [d["file_id"] for d in store.pending("us.eia.860m.history")] == ["newer"]
    store.release(
        "us.eia.860m.history", "2026-09", released_by="ops", now=dt.datetime(2026, 10, 24, tzinfo=dt.UTC)
    )
    assert store.load("us.eia.860m.history", "2026-09")["file_id"] == "newer"
    assert store.load_pending("us.eia.860m.history", "2026-09") is None
    assert not is_held(store.load("us.eia.860m.history", "2026-09"))


def test_a_held_month_is_served_only_once_released(tmp_path) -> None:
    store = MonthlyStore(tmp_path)
    store.write({**_doc(), "dq": {"status": "held", "checks": []}})
    assert is_held(store.load("us.eia.860m.history", "2026-09"))
    store.release(
        "us.eia.860m.history",
        "2026-09",
        released_by="data-engineer",
        now=dt.datetime(2026, 10, 24, tzinfo=dt.UTC),
    )
    released = store.load("us.eia.860m.history", "2026-09")
    assert not is_held(released) and released["dq"]["released_by"] == "data-engineer"
    with pytest.raises(DocumentError):
        store.release("us.eia.860m.history", "2026-09", released_by="x", now=dt.datetime.now(dt.UTC))


# ---------------------------------------------------------------------------------- tallies
def test_a_tally_is_exact_and_a_sum_over_nothing_is_null() -> None:
    tally = Tally()
    for mw in (0.1, 0.2, None):
        tally.add(mw)
    assert tally.as_dict() == {"count": 3, "mw": 0.3, "mw_stated": 2}
    assert Tally().as_dict("nameplate_mw") == {
        "count": 0,
        "mw": None,
        "mw_stated": 0,
        "nameplate_mw": None,
        "nameplate_mw_stated": 0,
    }
    block = Block(basis="b", dimensions=("technology",))
    block.add({"technology": None}, 1.0)
    assert block.as_dict()["by_technology"] == {"_unstated": {"count": 1, "mw": 1.0, "mw_stated": 1}}


def test_cell_readers() -> None:
    assert number(" ") is None and number("1,250.5") == 1250.5 and number(True) is None
    assert number(float("nan")) is None
    assert (
        year_of(2027) == 2027
        and year_of(dt.datetime(2030, 5, 1, tzinfo=dt.UTC)) == 2030
        and year_of(" ") is None
    )
    index, header, before = find_header(
        [["title"], ["\n \nNet Summer Capacity (MW)", "Plant ID"]], ("Plant ID",)
    )
    assert index == 1 and header[0] == "Net Summer Capacity (MW)" and before == [["title"]]


# -------------------------------------------------------------------------------------- dq
def _summary(rows: int, basis: str = "b", missing: int = 0) -> dict[str, object]:
    return {
        "period": "2026-09",
        "blocks": {"operating": {"basis": basis, "total": {"count": rows}}},
        "dq_facts": {"capacity_missing": {"operating": missing}},
    }


def test_dq_holds_an_empty_block_and_a_collapse_and_warns_on_drift() -> None:
    previous = {
        **_summary(1000),
        "period": "2026-08",
        "dq": {"facts": {"capacity_missing": {"operating": 0}}},
    }
    assert check_month(_summary(0), previous)["status"] == "held"
    assert check_month(_summary(400), previous)["status"] == "held"  # 60 % fewer rows
    assert check_month(_summary(1300), previous)["status"] == "warn"  # 30 % more
    assert check_month(_summary(1050), previous)["status"] == "pass"
    assert check_month(_summary(400, basis="other"), previous)["status"] == "pass"  # not like for like
    assert check_month(_summary(1000, missing=100), previous)["checks"][0]["check"] == "null_spike"
    small = {**_summary(4), "period": "2026-08"}
    assert check_month(_summary(10), small)["status"] == "pass"  # too few rows to judge a share


def test_dq_flags_vocabulary_drift_and_duplicate_keys() -> None:
    summary = {
        **_summary(10),
        "dq_facts": {
            "unknown_status": {"operating": ["XX"]},
            "duplicate_keys": {"operating": 2},
            "unmapped_technology": ["Mystery"],
        },
    }
    checks = {c["check"] for c in check_month(summary, None)["checks"]}
    assert checks == {"status_vocabulary", "duplicate_keys", "unmapped_technology"}


def test_dq_holds_a_month_filed_under_a_name_its_own_date_disputes() -> None:
    """ERCOT files a month by the report's name: a disputed name is held, never served. EIA files it
    by the workbook's own title, so there a disagreeing file name only warns."""
    by_name = {**_summary(10), "dq_facts": {"period_hint_mismatch": {"period_from": "name"}}}
    by_content = {**_summary(10), "dq_facts": {"period_hint_mismatch": {"period_from": "content"}}}
    assert check_month(by_name, None)["status"] == "held"
    assert check_month(by_content, None)["status"] == "warn"


def test_dq_holds_a_file_that_is_already_another_months_document() -> None:
    twin = {**_summary(10), "dq_facts": {"duplicate_file": ["2026-09"]}}
    dq = check_month(twin, None)
    assert dq["status"] == "held" and dq["checks"][0]["check"] == "duplicate_file"


def _retired(period: str, rows: int, mw: float, by_year: dict[str, tuple[int, float]]) -> dict[str, object]:
    return {
        "period": period,
        "blocks": {
            "retired": {
                "basis": "eia860m/retired/net-summer-mw/50-states-dc/retired-since-2002",
                "total": {"count": rows, "mw": mw},
                "by_retirement_year": {k: {"count": c, "mw": m} for k, (c, m) in by_year.items()},
            }
        },
        "dq_facts": {"cumulative": {"retired": {"dimension": "retirement_year", "open_from": "2025"}}},
    }


def test_dq_warns_when_a_list_that_only_grows_shrinks_or_restates_its_settled_years() -> None:
    """EIA's Retired sheet lists every retirement since 2002. In 2024-11 it left out 277 generators
    (5.2 GW) and put them back in 2024-12; in 2026-08 it added 25 old units (9.4 GW) that 2026-09
    took out again. Each such month is warned; an ordinary month of new retirements passes."""
    before = _retired("2026-07", 7300, 264_000.0, {"1974": (10, 1000.0), "2026": (40, 2000.0)})
    ordinary = _retired("2026-08", 7310, 264_400.0, {"1974": (10, 1000.0), "2026": (50, 2400.0)})
    assert check_month(ordinary, before)["status"] == "pass"
    restated = _retired("2026-08", 7325, 273_684.8, {"1974": (35, 10_400.0), "2026": (40, 2000.0)})
    assert [c["check"] for c in check_month(restated, before)["checks"]] == ["cumulative_restated"]
    shrank = _retired("2026-09", 7299, 264_000.0, {"1974": (10, 1000.0), "2026": (39, 1990.0)})
    assert [c["check"] for c in check_month(shrank, before)["checks"]] == ["cumulative_shrank"]
    # under the share, a settled year's correction is a month like any other
    small = 0.5 * RESTATED_SHARE * 264_000.0
    corrected = _retired(
        "2026-08", 7300, 264_000.0 + small, {"1974": (10, 1000.0 + small), "2026": (40, 2000.0)}
    )
    assert check_month(corrected, before)["status"] == "pass"


# ---------------------------------------------------------------------------------- runner
def test_auto_backfills_until_a_backfill_has_finished() -> None:
    assert effective_mode("auto", []) == "backfill"
    assert effective_mode("auto", [{"mode": "backfill", "status": "partial"}]) == "backfill"
    assert effective_mode("auto", [{"mode": "backfill", "status": "ok"}]) == "monthly"
    assert effective_mode("monthly", []) == "monthly"
    # a backfill that read another mirror record (or none) does not count for this one
    assert effective_mode("auto", [{"mode": "backfill", "status": "ok"}], "mirror:doi") == "backfill"
    done = [{"mode": "backfill", "status": "ok", "backfill_key": "mirror:doi"}]
    assert effective_mode("auto", done, "mirror:doi") == "monthly"


def test_consecutive_failures_stop_at_the_last_success() -> None:
    runs = [
        {"items": [{"key": "a", "status": "failed"}]},
        {"items": [{"key": "a", "status": "ok"}, {"key": "b", "status": "failed"}]},
        {"items": [{"key": "a", "status": "failed"}, {"key": "b", "status": "blocked"}]},
        {"items": [{"key": "a", "status": "failed"}, {"key": "b", "status": "skipped_repeated_failure"}]},
    ]
    assert consecutive_failures(runs) == {"a": 2, "b": 2}


def test_the_gate_refuses_what_could_never_be_published(registry, store) -> None:
    for reuse, publication, status in (
        ("restricted", "none", ""),
        ("open", "none", ""),
        ("open", "raw_ok", "pending"),
    ):
        entry = SourceEntry.from_yaml(
            {
                "id": "x.y",
                "reuse": reuse,
                "publication": publication,
                "status": status,
                "url": "https://x.test",
            }
        )
        with pytest.raises(HistoryGateRefused):
            gate(entry, frozenset({"open", "attribution"}))
    with pytest.raises(ValueError):
        run("us.eia.860m.history", "weekly", store=store, registry=registry)


def test_every_family_source_is_in_the_manifest_with_the_history_runner(registry) -> None:
    for source_id in HISTORY_CONNECTORS:
        entry = registry.get(source_id)
        assert entry.raw.get("runner") == "history"
        assert entry.reuse == "open" and entry.publication == "raw_ok"
        assert connector_class_for(source_id).source_id == source_id
        assert "+" in connector_class_for(source_id).version()
    with pytest.raises(KeyError):
        connector_class_for("us.iso.ercot.gen_queue")


# --------------------------------------------------------------------------------------- CLI
def test_the_cli_lists_refuses_and_releases(tmp_path, capsys) -> None:
    assert cli.main(["list"]) == 0
    assert "us.eia.860m.history" in capsys.readouterr().out
    assert cli.main(["run", "us.iso.ercot.gen_queue", "--mode", "monthly", "--data-dir", str(tmp_path)]) == 2
    MonthlyStore(tmp_path).write({**_doc(), "dq": {"status": "held"}})
    args = ["release", "us.eia.860m.history", "2026-09", "--by", "ops", "--data-dir", str(tmp_path)]
    assert cli.main(args) == 0
    assert cli.main(args) == 2  # no longer held


# ------------------------------------------------------------------------------------ retention
def test_every_months_snapshot_carries_an_extension_of_its_own(registry, store) -> None:
    """DA-10 compaction (`services/retention/snapshots.py`) keeps the newest snapshot of each file
    extension (the name after its first dot) and deletes other old ones it does not see in use. A
    history source's snapshot is a month's only bytes, and a restatement reads it again, so the runner
    names each one `<ts>.<n>-<YYYY-MM>.xlsx`: an extension of its own, the newest of its kind, never
    compacted (docs/24-trends.md section 9.3). This pins the history side of that contract; the
    retention module, which may not be imported from here (it reads private projects), is asked to
    read the monthly documents' `raw.snapshot` as in use (a follow-up outside this lane)."""
    from pipeline.connectors.history.conftest import ERCOT_FIXTURES, FakeHttp, clock

    routes = {
        "doclookupId=721696919": str(ERCOT_FIXTURES / "GIS_Report_December_2018_sample.xlsx"),
        "doclookupId=1281327706": str(ERCOT_FIXTURES / "GIS_Report_September2026_sample.xlsx"),
        "IceDocListJsonWS": str(ERCOT_FIXTURES / "ercot_gis_doclist_sample.json"),
    }
    sid = "us.iso.ercot.gen_queue.history"
    run(sid, "backfill", store=store, http=FakeHttp(routes), registry=registry, now=clock())
    names = [
        str(d["raw"]["snapshot"]).rsplit("/", 1)[-1] for d in MonthlyStore(store.base_root).documents(sid)
    ]
    assert len(names) == 2
    extensions = [name.split(".", 1)[1] for name in names]
    assert len(set(extensions)) == len(extensions)
    assert all(
        ext.endswith(f"-{d}.xlsx") for ext, d in zip(sorted(extensions), ("2018-12", "2026-09"), strict=True)
    )


def test_dq_warns_on_rows_with_no_technology_only_when_they_carry_capacity() -> None:
    """EIA lists one to nine rows with no Technology in 76 of 135 months, almost all small: those
    stay quiet (counted in `unknown`, the label `(blank)` recorded). A block whose blank rows carry
    100 MW or more (2026-09 Planned: four Willow Rock units, 500 MW) is warned, on that block."""
    facts = {
        "unmapped_technology": ["(blank)"],
        "technology_blank": {"planned": 4, "operating": 1},
        "technology_blank_mw": {"planned": 500.0, "operating": 2.2},
    }
    dq = check_month({**_summary(10), "dq_facts": facts}, None)
    assert [(c["check"], c["block"]) for c in dq["checks"]] == [("technology_blank", "planned")]
    quiet = {**facts, "technology_blank_mw": {"planned": 0.3, "operating": 2.2}}
    assert check_month({**_summary(10), "dq_facts": quiet}, None)["status"] == "pass"
    # a label the classifier cannot read still warns, whatever its size
    unread = {**quiet, "unmapped_technology": ["(blank)", "Mystery"]}
    assert [c["check"] for c in check_month({**_summary(10), "dq_facts": unread}, None)["checks"]] == [
        "unmapped_technology"
    ]
