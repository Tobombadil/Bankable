"""Parser tests for us.iso.caiso.gen_queue against a recorded Public Queue Report."""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot

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
