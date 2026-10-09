"""Parser tests for us.iso.caiso.gen_queue against a recorded Public Queue Report."""

from __future__ import annotations

import hashlib
import io

import openpyxl
import pandas as pd
import pytest

from conftest import connector_for, fixture_path, snapshot
from pipeline.connectors.base import ParseError
from pipeline.vendor.gridstatus import queues

SOURCE_ID = "us.iso.caiso.gen_queue"
URL = "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("caiso_public_queue_report.xlsx", URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_all_three_sheets_are_parsed(parsed):
    _, _, _, df = parsed
    assert len(df) == 39
    assert {"studied", "contracted", "built", "withdrawn"} & set(df["lifecycle_state"])
    assert set(df["status_raw"]) == {"ACTIVE", "COMPLETED", "WITHDRAWN"}


def test_provenance_and_licence(parsed):
    c, _, _, df = parsed
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert df["retrieved_at"].notna().all()


def test_queue_position_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert not df["record_id"].duplicated().any()
    assert set(df["source_record_id"]) == {str(r["Queue ID"]).strip() for r in rows}


def test_executed_agreement_promotes_active_rows_to_contracted(parsed):
    _, _, _, df = parsed
    executed = df[df["raw"].str.contains('"Interconnection Agreement Status": "Executed"')]
    active_executed = executed[executed["status_raw"] == "ACTIVE"]
    assert len(active_executed) >= 1
    assert set(active_executed["lifecycle_state"]) == {"contracted"}
    assert set(active_executed["status_rule"]) == {"caiso.active_ia_executed"}


def test_caiso_has_no_sponsor_column_so_sponsor_is_null(parsed):
    _, _, _, df = parsed
    assert df["sponsor_name"].isna().all()


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
    df = queues.caiso_queue(fixture_path("caiso_public_queue_report.xlsx").read_bytes())
    assert (df.shape, _frame_digest(df)) == ((39, 36), "8db2855cd8d87abe")


def test_a_renamed_column_fails_the_parse_closed():
    """A layout change surfaces as ParseError (upstream asserted; an assert vanishes under -O).
    Renamed in every sheet: the sheets are concatenated, so one sheet's rename alone leaves the
    column present (null for that sheet's rows), exactly as gridstatus did."""
    wb = openpyxl.load_workbook(fixture_path("caiso_public_queue_report.xlsx"))
    for ws in wb.worksheets:
        for cell in ws[4]:
            if cell.value == "Net MWs to Grid":
                cell.value = "Net MW to Grid"
    buf = io.BytesIO()
    wb.save(buf)
    c = connector_for(SOURCE_ID)
    raw = snapshot("caiso_public_queue_report.xlsx", URL)
    raw.content = buf.getvalue()
    with pytest.raises(ParseError, match="Net MWs to Grid"):
        c.parse(raw)
