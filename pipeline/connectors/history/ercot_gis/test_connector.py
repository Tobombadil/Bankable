"""us.iso.ercot.gen_queue.history: the parser on three real GIS reports (December 2018, the single
"Project Details" layout; March 2020, the same sheet with the excludes-Inactive note and a coal
steam turbine; September 2026, the Large/Small Gen split), the archive listing, and the runs:
corrections, a report posted under another month's name, shared reports, restatement and a failed
fetch, with recorded fixtures only."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import pytest

from pipeline.connectors.history import connector_class_for
from pipeline.connectors.history import runner as history_runner
from pipeline.connectors.history.base import MonthFile
from pipeline.connectors.history.conftest import (
    ERCOT_FIXTURES,
    RECORDED_AT,
    FakeHttp,
    clock,
    fixture_bytes,
    stored_run,
)
from pipeline.connectors.history.documents import MonthlyStore
from pipeline.connectors.history.dq import check_month
from pipeline.connectors.history.ercot_gis.connector import (
    DOC_LIST,
    DOWNLOAD,
    gis_documents,
    lifecycle,
    phase_token,
    report_period,
    technology,
    unlisted_codes,
)
from pipeline.connectors.history.runner import run

SID = "us.iso.ercot.gen_queue.history"
OLD = ERCOT_FIXTURES / "GIS_Report_December_2018_sample.xlsx"
MARCH_2020 = ERCOT_FIXTURES / "GIS_Report_March_2020_sample.xlsx"
CURRENT = ERCOT_FIXTURES / "GIS_Report_September2026_sample.xlsx"
LISTING = ERCOT_FIXTURES / "ercot_gis_doclist_sample.json"
DEC_2018 = "721696919"
SEP_2026 = "1281327706"
AUG_2026 = "1269363208"


def _month(path, hint: str) -> MonthFile:
    return MonthFile(
        content=fixture_bytes(path),
        source_url=DOWNLOAD.format(doc_id="x"),
        retrieved_at=RECORDED_AT,
        route="origin",
        item_key="test",
        period_hint=hint,
        via={"doc_id": "x"},
    )


@pytest.fixture()
def connector(registry, store):
    cls = connector_class_for(SID)
    return cls(registry.get(SID), FakeHttp({}), store=store, clock=clock())


def _routes(listing: bytes | None = None, **extra: object) -> dict[str, object]:
    routes: dict[str, object] = {
        f"doclookupId={DEC_2018}": str(OLD),
        f"doclookupId={SEP_2026}": str(CURRENT),
        f"doclookupId={AUG_2026}": str(CURRENT),
        "IceDocListJsonWS": listing if listing is not None else str(LISTING),
    }
    routes.update(extra)
    return routes


# --------------------------------------------------------------------------------- the parser
def test_the_single_sheet_layout(connector) -> None:
    out = connector.summarise(_month(OLD, "2018-12"))
    assert out["period"] == "2018-12"
    layout = out["layout"]
    assert layout["arrangement"] == "single"
    assert layout["header_rows"] == {"projects": 21}
    assert layout["as_of"] == "2018-12-31"
    projects = out["blocks"]["projects"]
    assert projects["basis"] == "ercot-gis/details-all-sizes/capacity-mw/fis-requested"
    assert projects["total"]["count"] == sum(t["count"] for t in projects["by_status"].values())
    assert set(projects["by_status"]) <= {"studied", "contracted", "under_construction", "built"}
    assert set(projects["by_technology"]) <= {
        "wind",
        "solar",
        "storage",
        "gas_cc",
        "gas_ct",
        "gas_ice",
        "gas_steam",
    }
    assert "small_generators" not in out["blocks"]
    assert out["dq_facts"]["rows"]["projects"] == projects["total"]["count"]  # sub-header rows skipped


def test_the_split_layout_keeps_small_generators_apart(connector) -> None:
    out = connector.summarise(_month(CURRENT, "2026-09"))
    assert out["layout"]["arrangement"] == "split"
    assert out["layout"]["as_of"] == "2026-09-30"
    projects = out["blocks"]["projects"]
    assert projects["basis"] == "ercot-gis/details-large-gen/capacity-mw/fis-requested/excl-inactive"
    small = out["blocks"]["small_generators"]
    assert small["basis"] == "ercot-gis/details-small-gen/capacity-mw/excl-inactive"
    assert "phase" not in {k[3:] for k in small if k.startswith("by_")}
    assert "dq_facts" in out and out["dq_facts"]["unmapped_technology"] == []
    # Lynx Energy Storage is listed as `MWH SO`, codes ERCOT's own list does not hold: storage, warned
    assert out["technology_labels"]["MWH SO"] == "storage"
    assert out["dq_facts"]["unlisted_codes"] == {"fuel:MWH": 1, "technology:SO": 1}
    assert [c["check"] for c in check_month(out, None)["checks"]] == ["code_vocabulary"]


def test_a_name_that_disagrees_with_the_summary_date_holds_the_month(connector) -> None:
    """The month comes from the report's name, so a name the report's own date disputes is never
    served under that name: the month is held, not warned (a warned document is served)."""
    out = connector.summarise(_month(CURRENT, "2026-08"))
    assert out["period"] == "2026-08"
    assert out["dq_facts"]["period_hint_mismatch"] == {
        "report_name": "2026-08",
        "summary_as_of": "2026-09-30",
        "period_from": "name",
    }
    dq = check_month(out, None)
    assert dq["status"] == "held"
    assert ("period_mismatch", "hold") in [(c["check"], c["level"]) for c in dq["checks"]]


def test_coal_is_read_from_the_fuel_on_a_real_report(connector) -> None:
    """March 2020: ERCOT lists a 493 MW coal steam turbine as `COAL ST`. Read through gridstatus's
    label and the classifier's prime-mover rules it was `gas_steam` (553 MW that month, 493 of them
    this unit); its fuel names it coal."""
    out = connector.summarise(_month(MARCH_2020, "2020-03"))
    projects = out["blocks"]["projects"]
    assert projects["basis"] == "ercot-gis/details-all-sizes/capacity-mw/fis-requested/excl-inactive"
    assert out["layout"]["as_of"] == "2020-03-31" and "period_hint_mismatch" not in out["dq_facts"]
    assert out["technology_labels"]["COAL ST"] == "coal"
    assert projects["by_technology"]["coal"] == {"count": 1, "mw": 493.0, "mw_stated": 1}
    assert projects["by_technology"]["gas_steam"]["mw"] == 0.0  # the gas steam row left in the sample
    # `COAL` is not one of ERCOT's own codes (`COA` is): read, and warned
    assert out["dq_facts"]["unlisted_codes"] == {"fuel:COAL": 1}
    assert [c["check"] for c in check_month(out, None)["checks"]] == ["code_vocabulary"]


#: Every fuel and technology pair the GIS archive holds, December 2018 to September 2026 (read from
#: all 94 monthly reports on 2026-10-23), and the bucket each reads as.
ARCHIVE_LABELS: dict[tuple[str, str], str] = {
    ("BAT", "BA"): "storage",
    ("BAT", "BL"): "storage",
    ("BAT", "BO"): "storage",
    ("BIO", "ST"): "biomass",
    ("COA", "ST"): "coal",
    ("COAL", "ST"): "coal",
    ("GAS", "CC"): "gas_cc",
    ("GAS", "GT"): "gas_ct",
    ("GAS", "IC"): "gas_ice",
    ("GAS", "OT"): "gas_other",
    ("GAS", "ST"): "gas_steam",
    ("HYD", "OT"): "hydrogen",
    ("MWH", "OT"): "storage",
    ("MWH", "SO"): "storage",
    ("NUC", "ST"): "nuclear",
    ("OIL", "GT"): "oil",
    ("OIL", "IC"): "oil",
    ("OIL", "OT"): "oil",
    ("OTH", "BA"): "storage",
    ("OTH", "EN"): "storage",
    ("OTH", "OT"): "other",
    ("OTH", "PV"): "solar",
    ("SOL", "CP"): "solar_thermal",
    ("SOL", "PV"): "solar",
    ("WAT", "OT"): "hydro",
    ("WIN", "WT"): "wind",
}


@pytest.mark.parametrize(("codes", "bucket"), sorted(ARCHIVE_LABELS.items()))
def test_every_label_in_the_archive_reads_its_technology(codes: tuple[str, str], bucket: str) -> None:
    assert technology(*codes) == bucket


def test_the_archive_holds_26_labels_and_the_unlisted_codes_are_named() -> None:
    assert len(ARCHIVE_LABELS) == 26
    unlisted = sorted({code for pair in ARCHIVE_LABELS for code in unlisted_codes(*pair)})
    assert unlisted == [
        "fuel:BAT",
        "fuel:COAL",
        "fuel:MWH",
        "technology:BL",
        "technology:BO",
        "technology:SO",
    ]
    # the codes on ERCOT's list that the archive has not used yet read by the same rule
    assert technology("GEO", "ST") == "geothermal" and technology("PET", "ST") == "oil"
    assert technology("WAT", "HY") == "hydro" and technology("GAS", "FC") == "fuel_cell"
    assert technology("HYD", "GT") == "hydrogen" and technology("BIO", "IC") == "biomass"


def test_the_register_rules() -> None:
    when = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert lifecycle(when, None, when) == "built"
    assert lifecycle(None, when, when) == "under_construction"
    assert lifecycle(None, None, when) == "contracted"
    assert lifecycle(None, None, None) == "studied"
    assert (
        lifecycle(dt.datetime(1900, 1, 1, tzinfo=dt.UTC), None, None) == "built"
    )  # ERCOT's "date not available"
    assert lifecycle(None, " ", "N/A") == "studied"
    assert technology("OTH", "BA") == "storage"
    assert technology("SOL", "PV") == "solar"
    assert technology("GAS", "CC") == "gas_cc"
    assert technology(None, None) == "unknown"
    assert phase_token("SS Completed, FIS Started, No IA") == "ss_completed_fis_started_no_ia"


def test_report_names_and_the_month_each_listing_keeps() -> None:
    assert report_period("RPT.x.GIS_Report_June_2020_Correction.xlsx") == ("2020-06", "Correction")
    assert report_period("RPT.x.GIS_Report_September2024.xlsx") == ("2024-09", None)
    assert report_period("RPT.x.GIS_Report_Jun2026.xlsx") == ("2026-06", None)
    assert report_period("RPT.x.Co-located_Battery_Identification_Report_August_2026.xlsx") is None
    months = gis_documents(json.loads(fixture_bytes(LISTING)))
    assert list(months) == [
        "2018-12",
        "2019-01",
        "2020-04",
        "2020-06",
        "2020-07",
        "2024-09",
        "2026-06",
        "2026-08",
        "2026-09",
    ]
    assert months["2020-07"]["variant"] == "CORRECTION2"  # the latest re-issue is the month
    assert months["2020-04"]["variant"] == "revised"


def test_reissues_of_one_day_are_ordered_by_instant_not_by_text() -> None:
    """ERCOT writes `PublishDate` in Central time with its offset. On 2 November 2025 (CDT ends)
    `10:30-05:00` is 15:30Z and `10:00-06:00` is 16:00Z: the second is later, though its text sorts
    first."""

    def doc(name: str, doc_id: str, published: str) -> dict[str, Any]:
        return {
            "Document": {
                "ConstructedName": name,
                "Extension": "xlsx",
                "DocID": doc_id,
                "PublishDate": published,
            }
        }

    listing = {
        "ListDocsByRptTypeRes": {
            "DocumentList": [
                doc("RPT.1.GIS_Report_October2025.xlsx", "100", "2025-11-02T10:30:00-05:00"),
                doc("RPT.2.GIS_Report_October2025_Corrected.xlsx", "101", "2025-11-02T10:00:00-06:00"),
            ]
        }
    }
    assert gis_documents(listing)["2025-10"]["doc_id"] == "101"
    listing["ListDocsByRptTypeRes"]["DocumentList"].reverse()
    assert gis_documents(listing)["2025-10"]["doc_id"] == "101"


# ---------------------------------------------------------------------------------- the runs
def test_a_backfill_reads_what_it_can_and_records_the_rest(registry, store) -> None:
    http = FakeHttp(_routes())
    result = run(SID, "backfill", store=store, http=http, registry=registry, now=clock())
    record = result.record
    assert record["status"] == "partial"  # six months' downloads answer 404
    assert sorted(record["documents"]["written"]) == ["2018-12", "2026-08", "2026-09"]
    monthly = MonthlyStore(store.base_root)
    doc = monthly.load(SID, "2026-09")
    assert doc["file_id"] == SEP_2026
    assert doc["source_url"] == DOWNLOAD.format(doc_id=SEP_2026)
    assert doc["credit"] == "Source: ERCOT"
    assert doc["retrieved_from"]["publish_date"].startswith("2026-10-01")
    assert all(
        not c["honour_robots"] for c in http.calls
    )  # the MIS routes, as the queue connector reads them
    # the August document here is September's bytes under August's name: written for August, as its
    # name says, and held (its own date says September), so it is never served as August; the same
    # bytes are September's own month, which passes (a held document is not a served twin)
    august = monthly.load(SID, "2026-08")
    assert august["dq"]["status"] == "held"
    assert {c["check"]: c["level"] for c in august["dq"]["checks"]} == {
        "period_mismatch": "hold",
        "code_vocabulary": "warn",
    }
    assert monthly.load(SID, "2026-09")["dq"]["status"] == "warn"  # served; only the MWH code warned
    assert record["awaiting_release"] == ["2026-08"]
    assert monthly.load(SID, "2018-12")["blocks"]["projects"]["basis"].endswith("/fis-requested")


def _good_august(store, september: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """The month's served document as an earlier run left it: August's own report (`AUG_2026`)."""
    august = json.loads(json.dumps(september))
    august.update(period="2026-08", file_id=AUG_2026, source_url=DOWNLOAD.format(doc_id=AUG_2026))
    august["raw"] = {**august["raw"], "sha256": "a" * 64}
    august["layout"]["as_of"] = "2026-08-31"
    MonthlyStore(store.base_root).write(august)
    return august


def test_a_report_posted_under_another_months_name_is_held_beside_the_real_month(registry, store) -> None:
    """ERCOT re-posts September 2026 as an August correction (its names are typed by hand). The
    re-post is held, for its date and for being September's file, beside August's own document,
    which stays served: August keeps its figures, and the Aug->Sep change is not read as zero. The
    next run neither downloads nor parses it again; an operator's release would make it the month."""

    def missing(**_: object) -> tuple[int, bytes, dict[str, str]]:
        return 404, b"", {}

    run(
        SID,
        "backfill",
        store=store,
        http=FakeHttp(_routes(**{f"doclookupId={AUG_2026}": missing})),
        registry=registry,
        now=clock(),
    )
    monthly = MonthlyStore(store.base_root)
    _good_august(store, monthly.load(SID, "2026-09"))
    listing = json.loads(fixture_bytes(LISTING))
    listing["ListDocsByRptTypeRes"]["DocumentList"].append(
        {
            "Document": {
                "ConstructedName": "RPT.00015933.20261020.1.GIS_Report_August2026_CORRECTION.xlsx",
                "Extension": "xlsx",
                "DocID": "1295000000",
                "PublishDate": "2026-10-20T16:00:00-05:00",
            }
        }
    )
    body = json.dumps(listing).encode()
    second = run(
        SID,
        "monthly",
        store=store,
        http=FakeHttp(_routes(body, **{"doclookupId=1295000000": str(CURRENT)})),
        registry=registry,
        now=clock(),
    )
    assert second.record["status"] == "partial" and second.record["documents"]["held"] == ["2026-08"]
    assert second.record["documents"]["replaced"] == []
    served = monthly.load(SID, "2026-08")
    assert served["file_id"] == AUG_2026 and served["dq"]["status"] != "held"  # the last good August stands
    pending = monthly.load_pending(SID, "2026-08")
    assert pending is not None and pending["file_id"] == "1295000000"
    assert {c["check"]: c["level"] for c in pending["dq"]["checks"]} == {
        "period_mismatch": "hold",
        "duplicate_file": "hold",
        "code_vocabulary": "warn",
    }
    assert pending["dq"]["facts"]["duplicate_file"] == ["2026-09"]
    assert second.record["awaiting_release"] == ["2026-08"]

    http = FakeHttp(_routes(body, **{"doclookupId=1295000000": str(CURRENT)}))
    third = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert not [c for c in http.calls if "doclookupId=1295000000" in c["url"]]  # not downloaded again
    assert "2026-08" not in third.record["documents"]["held"] + third.record["documents"]["replaced"]
    assert third.record["awaiting_release"] == ["2026-08"]  # still waiting on an operator

    monthly.release(SID, "2026-08", released_by="ops", now=dt.datetime(2026, 10, 24, tzinfo=dt.UTC))
    released = monthly.load(SID, "2026-08")
    assert released["file_id"] == "1295000000" and released["dq"]["released_by"] == "ops"
    assert monthly.load_pending(SID, "2026-08") is None


def test_a_second_run_is_unchanged_and_asks_only_for_the_listing(registry, store) -> None:
    run(SID, "backfill", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    http = FakeHttp(_routes())
    second = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert second.record["documents"]["written"] == []
    assert not [c for c in http.calls if f"doclookupId={DEC_2018}" in c["url"]]


def test_a_correction_replaces_its_month_and_a_failed_one_keeps_the_last_good(registry, store) -> None:
    run(SID, "backfill", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    listing = json.loads(fixture_bytes(LISTING))
    corrected = {
        "Document": {
            "ConstructedName": "RPT.00015933.20261015.1.GIS_Report_September2026_Corrected.xlsx",
            "Extension": "xlsx",
            "DocID": "1290000000",
            "PublishDate": "2026-10-15T10:00:00-05:00",
            "FriendlyName": "GIS_Report_September2026_Corrected",
        }
    }
    listing["ListDocsByRptTypeRes"]["DocumentList"].append(corrected)
    body = json.dumps(listing).encode()

    def broken(**_: object) -> tuple[int, bytes, dict[str, str]]:
        return 500, b"", {}

    failed = run(
        SID,
        "monthly",
        store=store,
        http=FakeHttp(_routes(body, **{"doclookupId=1290000000": broken})),
        registry=registry,
        now=clock(),
    )
    assert failed.record["status"] == "failed"  # every download failed; nothing was replaced
    monthly = MonthlyStore(store.base_root)
    assert monthly.load(SID, "2026-09")["file_id"] == SEP_2026  # the last good document stands

    fixed = run(
        SID,
        "monthly",
        store=store,
        http=FakeHttp(_routes(body, **{"doclookupId=1290000000": str(CURRENT)})),
        registry=registry,
        now=clock(),
    )
    doc = monthly.load(SID, "2026-09")
    assert doc["file_id"] == "1290000000"
    assert doc["retrieved_from"]["variant"] == "Corrected"
    assert "2026-09" in fixed.record["documents"]["unchanged"] + fixed.record["documents"]["replaced"]


def test_a_report_the_queue_connector_stored_is_not_downloaded_again(registry, store) -> None:
    key = stored_run(
        store,
        "us.iso.ercot.gen_queue",
        fixture_bytes(CURRENT),
        url=DOWNLOAD.format(doc_id=SEP_2026),
        meta={"doc_id": SEP_2026, "publish_date": "2026-10-01T16:08:28-05:00"},
    )
    http = FakeHttp(_routes())
    run(SID, "backfill", store=store, http=http, registry=registry, now=clock())
    assert not [c for c in http.calls if f"doclookupId={SEP_2026}" in c["url"]]
    doc = MonthlyStore(store.base_root).load(SID, "2026-09")
    assert doc["retrieved_from"]["route"] == "shared" and doc["raw"]["snapshot"] == key


def test_a_parser_change_restates_every_month_from_its_snapshot(registry, store, monkeypatch) -> None:
    run(SID, "backfill", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    cls = connector_class_for(SID)
    monkeypatch.setattr(cls, "version", classmethod(lambda _cls: "9.9.9+restated"))
    http = FakeHttp(_routes())
    result = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert sorted(result.record["documents"]["replaced"]) == ["2018-12", "2026-08", "2026-09"]
    assert [c["url"] for c in http.calls] == [DOC_LIST] + [
        c["url"] for c in http.calls if "doclookupId" in c["url"]
    ]
    assert not [c for c in http.calls if f"doclookupId={DEC_2018}" in c["url"]]  # read from the store
    restated = MonthlyStore(store.base_root).load(SID, "2018-12")
    assert restated["parser_version"] == "9.9.9+restated"
    # How the bytes first arrived stays on the document beside the snapshot the restatement read,
    # and what the listing says of the report (its name and publication date) with it.
    came = restated["retrieved_from"]
    assert came["route"] == "origin" and str(came["doc_id"]) == DEC_2018
    assert came["restated_from"] == restated["raw"]["snapshot"]
    assert restated["published"] == "2020-06-29T15:45:17-05:00"
    assert (
        came["name"].endswith("GIS_Report_December_2018.xlsx")
        and came["publish_date"] == restated["published"]
    )


def test_a_restatement_keeps_the_publication_date_a_document_lost(registry, store, monkeypatch) -> None:
    """Documents an earlier runner wrote without `published` (and without the listing's name) get both
    back on their next restatement, from the listing entry for the same DocID."""
    run(SID, "backfill", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    monthly = MonthlyStore(store.base_root)
    stripped = monthly.load(SID, "2026-09")
    stripped["published"] = None
    stripped["retrieved_from"] = {"route": "origin", "item": f"doc:{SEP_2026}", "doc_id": SEP_2026}
    monthly.write(stripped)
    monkeypatch.setattr(connector_class_for(SID), "version", classmethod(lambda _cls: "9.9.9+restated"))
    run(SID, "monthly", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    restated = monthly.load(SID, "2026-09")
    assert restated["published"] == restated["retrieved_from"]["publish_date"]
    assert restated["published"].startswith("2026-10-01")
    assert restated["retrieved_from"]["name"].endswith("GIS_Report_September2026.xlsx")


def test_a_restatement_whose_snapshot_is_gone_reads_the_report_again(registry, store, monkeypatch) -> None:
    """No stored copy of December 2018 is left (a lost volume, a retention run): the restatement
    downloads the same DocID from the MIS instead of failing and leaving the month under its old
    parser."""
    run(SID, "backfill", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    monthly = MonthlyStore(store.base_root)
    gone = monthly.load(SID, "2018-12")["raw"]["snapshot"]
    store.backend.delete(gone)
    monkeypatch.setattr(connector_class_for(SID), "version", classmethod(lambda _cls: "9.9.9+restated"))
    http = FakeHttp(_routes())
    result = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert "2018-12" in result.record["documents"]["replaced"]
    assert [c for c in http.calls if f"doclookupId={DEC_2018}" in c["url"]]
    doc = monthly.load(SID, "2018-12")
    assert doc["parser_version"] == "9.9.9+restated" and doc["raw"]["snapshot"] != gone
    assert store.backend.exists(doc["raw"]["snapshot"])
    assert doc["raw"]["sha256"] == hashlib.sha256(fixture_bytes(OLD)).hexdigest()


def test_an_item_failing_three_runs_running_is_skipped_until_asked(registry, store) -> None:
    for _ in range(3):
        run(SID, "monthly", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    fourth = run(SID, "monthly", store=store, http=FakeHttp(_routes()), registry=registry, now=clock())
    skipped = [i for i in fourth.record["items"] if i["status"] == "skipped_repeated_failure"]
    assert {i["periods"][0] for i in skipped} == {
        "2019-01",
        "2020-04",
        "2020-06",
        "2020-07",
        "2024-09",
        "2026-06",
    }
    again = run(
        SID,
        "monthly",
        store=store,
        http=FakeHttp(_routes()),
        registry=registry,
        now=clock(),
        retry_failed=True,
    )
    assert not [i for i in again.record["items"] if i["status"] == "skipped_repeated_failure"]


def test_the_listing_failing_fails_the_run_and_writes_nothing(registry, store) -> None:
    def down(**_: object) -> tuple[int, bytes, dict[str, str]]:
        return 503, b"", {}

    result = run(
        SID, "auto", store=store, http=FakeHttp({"IceDocListJsonWS": down}), registry=registry, now=clock()
    )
    assert result.record["status"] == "failed"
    assert MonthlyStore(store.base_root).periods(SID) == []


def test_a_large_file_is_summarised_in_a_child_process(connector) -> None:
    month = _month(CURRENT, "2026-09")
    inline = connector.summarise(month)
    forked = history_runner.summarise(connector, month, isolate_bytes=1)
    assert forked == inline
