"""Parser tests for us.iso.ercot.gen_queue against a recorded GIS Report (docs/04 E-6), and the
unchanged poll: a listing that names the stored DocID costs one request, not a download."""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
from typing import Any

import pandas as pd
import pytest

from conftest import connector_for, fixture_path, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.connectors.us_iso_ercot_gen_queue.connector import DOC_LIST, DOWNLOAD, latest_gis_document
from pipeline.vendor.gridstatus import queues

SOURCE_ID = "us.iso.ercot.gen_queue"
URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1269363208"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ercot_gis_report.xlsx", URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_parse_returns_source_shaped_rows(parsed):
    _, _, rows, _ = parsed
    assert len(rows) == 25
    assert {"Queue ID", "Project Name", "Status", "Capacity (MW)", "Generation Type"} <= set(rows[0])


def test_canonical_record_carries_the_provenance_quartet(parsed):
    c, raw, _, df = parsed
    assert len(df) == 25
    for col in ("source_id", "source_url", "retrieved_at", "licence_id"):
        assert df[col].notna().all()
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["source_url"]) == {URL}
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert df["retrieved_at"].iloc[0] == raw.retrieved_at_iso


def test_record_id_is_the_queue_id_and_unique(parsed):
    _, _, rows, df = parsed
    assert not df["record_id"].duplicated().any()
    assert df["record_id"].iloc[0] == f"{SOURCE_ID}:{df['source_record_id'].iloc[0]}"
    assert set(df["source_record_id"]) == {str(r["Queue ID"]).strip() for r in rows}


def test_status_harmonisation_uses_the_ercot_rules(parsed):
    _, _, rows, df = parsed
    assert set(df["lifecycle_state"]) <= {"studied", "contracted", "under_construction", "built"}
    assert df["status_rule"].str.startswith("ercot.").all()
    # The fixture's 25 rows: 2 Active with no milestone, 4 with only an IA, 19 synchronised.
    assert df["lifecycle_state"].value_counts().to_dict() == {"built": 19, "contracted": 4, "studied": 2}
    # built only where ERCOT approved synchronisation; "Completed" alone (IA signed) is contracted
    synced = [pd.notna(r.get("Approved for Synchronization")) for r in rows]
    assert ((df["lifecycle_state"] == "built") == pd.Series(synced)).all()
    ia_only = (df["status_raw"] == "Completed") & (df["status_rule"] == "ercot.ia_signed")
    assert (df.loc[ia_only, "lifecycle_state"] == "contracted").all() and ia_only.sum() == 4
    # status_raw stays gridstatus's label, never the canonical state
    assert set(df["status_raw"]) == {"Completed", "Active"}


def test_restate_status_reads_each_stored_rows_raw_payload(parsed):
    c, _, _, df = parsed
    stored = df.copy()
    stored.loc[stored["status_raw"] == "Completed", "lifecycle_state"] = "built"  # the pre-2026-09-30 map
    restated = c.restate_status(stored)
    assert restated is not None
    assert list(restated.index) == list(stored.index)
    assert (restated["lifecycle_state"] == df["lifecycle_state"]).all()
    assert (restated["status_rule"] == df["status_rule"]).all()


def test_raw_payload_is_kept_per_row(parsed):
    _, _, rows, df = parsed
    import json

    payload = json.loads(df["raw"].iloc[0])
    assert payload["Queue ID"] == str(rows[0]["Queue ID"])
    assert "Generation Type" in payload


def test_technology_and_capacity_are_normalised(parsed):
    _, _, _, df = parsed
    assert df["technology"].notna().all()
    assert set(df["kind"]) <= {"generation", "storage", "transmission", "load", "other"}
    assert (df["capacity_mw"].dropna() > 0).all()


def test_parse_rejects_a_non_xlsx_payload():
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2.json", URL)
    with pytest.raises(ParseError):
        c.parse(raw)


def test_latest_gis_document_picks_the_newest_report_not_the_battery_report():
    listing = {
        "ListDocsByRptTypeRes": {
            "DocumentList": [
                {
                    "Document": {
                        "ConstructedName": "RPT.x.Co-located_Battery_Identification_Report_August_2026.xlsx",
                        "Extension": "xlsx",
                        "PublishDate": "2026-09-09T10:23:32-05:00",
                        "DocID": "1",
                    }
                },
                {
                    "Document": {
                        "ConstructedName": "RPT.x.GIS_Report_July2026.xlsx",
                        "Extension": "xlsx",
                        "PublishDate": "2026-08-01T14:38:04-05:00",
                        "DocID": "2",
                    }
                },
                {
                    "Document": {
                        "ConstructedName": "RPT.x.GIS_Report_August2026.xlsx",
                        "Extension": "xlsx",
                        "PublishDate": "2026-09-01T14:38:04-05:00",
                        "DocID": "3",
                    }
                },
            ]
        }
    }
    assert latest_gis_document(listing)["DocID"] == "3"


def test_latest_gis_document_raises_when_the_listing_has_none():
    with pytest.raises(ParseError):
        latest_gis_document({"ListDocsByRptTypeRes": {"DocumentList": []}})


# The bare parser frame over the committed fixture, pinned to what gridstatus 0.36.0's
# `get_interconnection_queue` returned for the same bytes before the library was dropped
# (2026-10-07, pipeline/vendor/gridstatus/README.md): shape, column order, dtypes, every value.
def _frame_digest(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(repr(list(df.columns)).encode())
    h.update(repr([str(t) for t in df.dtypes]).encode())
    h.update(pd.util.hash_pandas_object(df, index=True).values.tobytes())
    return h.hexdigest()[:16]


def test_vendored_parser_reproduces_the_gridstatus_0_36_frame():
    df = queues.ercot_queue(fixture_path("ercot_gis_report.xlsx").read_bytes())
    assert (df.shape, _frame_digest(df)) == ((25, 35), "28c0519524929638")


# ------------------------------------------------------------------ unchanged poll (DocID reuse)
#: The MIS document list in the shape `IceDocListJsonWS?reportTypeId=15933` answers (the fields
#: `latest_gis_document` reads). No listing response is recorded in tests/fixtures, so the documents
#: are the two GIS reports this connector has stored: August 2026 (DocID 1269363208, the recorded
#: `ercot_gis_report.xlsx`) and September 2026 (DocID 1281327706, published
#: 2026-10-01T16:08:28-05:00, per the 2026-10-09 run record), beside the co-located battery report
#: the connector skips.
AUGUST = {
    "ConstructedName": "rpt.00015933.0000000000000000.GIS_Report_August2026.xlsx",
    "FriendlyName": "GIS_Report_August2026",
    "Extension": "xlsx",
    "PublishDate": "2026-09-01T14:38:04-05:00",
    "DocID": "1269363208",
}
SEPTEMBER = {
    "ConstructedName": "rpt.00015933.0000000000000000.GIS_Report_September2026.xlsx",
    "FriendlyName": "GIS_Report_September2026",
    "Extension": "xlsx",
    "PublishDate": "2026-10-01T16:08:28-05:00",
    "DocID": "1281327706",
}
BATTERY = {
    "ConstructedName": (
        "rpt.00015933.0000000000000000.Co-located_Battery_Identification_Report_September_2026.xlsx"
    ),
    "FriendlyName": "Co-located_Battery_Identification_Report_September_2026",
    "Extension": "xlsx",
    "PublishDate": "2026-10-09T10:23:32-05:00",
    "DocID": "1283000001",
}


def listing(*docs: dict[str, str]) -> dict[str, object]:
    return {"ListDocsByRptTypeRes": {"DocumentList": [{"Document": d} for d in docs]}}


class _Answer:
    def __init__(self, status: int, content: bytes, body: object = None) -> None:
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": "application/octet-stream"}
        self._body = body

    def json(self) -> object:
        return self._body


class _Mis:
    """ERCOT MIS: the document list, and `mirDownload` serving the recorded workbook for any DocID."""

    def __init__(self, docs: list[dict[str, str]]) -> None:
        self.docs = docs
        self.workbook = fixture_path("ercot_gis_report.xlsx").read_bytes()
        self.urls: list[str] = []

    def get(self, url: str, **_: object) -> _Answer:
        self.urls.append(url)
        if url == DOC_LIST:
            return _Answer(200, b"{}", listing(*self.docs))
        return _Answer(200, self.workbook)

    def downloads(self) -> list[str]:
        return [u for u in self.urls if u != DOC_LIST]


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> Store:
    return Store(tmp_path)


#: Daily polls, one per day from 2 October 2026 (the runner's clock, which `fetch` reads).
DAY0 = dt.datetime(2026, 10, 2, 5, 21, tzinfo=dt.UTC)


def _ercot_run(store: Store, mis: _Mis, day: int = 0) -> Any:
    now = DAY0 + dt.timedelta(days=day)
    return run(SOURCE_ID, registry=Registry(), store=store, http=mis, now=now)  # type: ignore[arg-type]


def test_an_unchanged_listing_reuses_the_stored_report_without_downloading_it(store: Store) -> None:
    """A daily poll of a monthly report: the second day's listing names the same DocID, so the run
    costs the listing alone and ends `unchanged` against the stored workbook."""
    mis = _Mis([AUGUST, BATTERY])
    first = _ercot_run(store, mis)
    assert first.status == "ok", first.run.get("error")
    assert first.run["snapshot"]["meta"]["doc_id"] == "1269363208"
    assert first.run["snapshot"]["requests_made"] == 2
    assert mis.downloads() == [DOWNLOAD.format(doc_id="1269363208")]

    second = _ercot_run(store, mis, 1)
    assert second.status == "unchanged", second.run.get("error")
    assert mis.urls[-1] == DOC_LIST and len(mis.downloads()) == 1, "the workbook was not downloaded again"
    snap = second.run["snapshot"]
    assert snap["requests_made"] == 1 and snap["sha256"] == first.run["snapshot"]["sha256"]
    assert snap["fetched_url"] == DOWNLOAD.format(doc_id="1269363208")
    assert snap["meta"]["doc_id"] == "1269363208"
    assert snap["meta"]["reused"]["run_id"] == first.run["id"]
    assert snap["meta"]["reused"]["sha256"] == first.run["snapshot"]["sha256"]

    # A third unchanged day reuses again: the unchanged run's record carries the DocID forward.
    third = _ercot_run(store, mis, 2)
    assert third.status == "unchanged" and len(mis.downloads()) == 1


def test_a_new_document_in_the_listing_is_downloaded(store: Store) -> None:
    mis = _Mis([AUGUST])
    assert _ercot_run(store, mis).status == "ok"
    mis.docs = [AUGUST, SEPTEMBER, BATTERY]  # the September report lands on 1 October
    second = _ercot_run(store, mis, 1)
    assert mis.downloads()[-1] == DOWNLOAD.format(doc_id="1281327706")
    assert len(mis.downloads()) == 2
    assert second.run["snapshot"]["meta"]["doc_id"] == "1281327706"
    assert "reused" not in second.run["snapshot"]["meta"]


def test_the_same_docid_republished_is_downloaded(store: Store) -> None:
    mis = _Mis([AUGUST])
    assert _ercot_run(store, mis).status == "ok"
    mis.docs = [{**AUGUST, "PublishDate": "2026-09-02T09:00:00-05:00"}]
    _ercot_run(store, mis, 1)
    assert len(mis.downloads()) == 2


def test_a_stored_report_that_fails_its_hash_is_downloaded(store: Store) -> None:
    mis = _Mis([AUGUST])
    first = _ercot_run(store, mis)
    pathlib.Path(first.run["snapshot"]["object_key"]).write_bytes(b"PK corrupted")
    second = _ercot_run(store, mis, 1)
    assert second.status != "failed", second.run.get("error")
    assert len(mis.downloads()) == 2
    assert "reused" not in second.run["snapshot"]["meta"]


def test_fetch_downloads_when_there_is_no_previous_snapshot() -> None:
    mis = _Mis([AUGUST])
    c = connector_for(SOURCE_ID, http=mis)
    assert c.previous is None
    raw = c.fetch()
    assert raw.requests_made == 2 and raw.meta["doc_id"] == "1269363208"
    assert len(mis.downloads()) == 1 and "reused" not in raw.meta
