"""Parser tests for us.iso.nyiso.gen_queue against a recorded queue workbook."""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.us_iso_nyiso_gen_queue.connector import _identified

SOURCE_ID = "us.iso.nyiso.gen_queue"
URL = "https://www.nyiso.com/documents/20142/1407078/NYISO-Interconnection-Queue.xlsx"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("nyiso_interconnection_queue.xlsx", URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_active_withdrawn_and_in_service_sheets_all_appear(parsed):
    _, _, _, df = parsed
    assert {"studied", "withdrawn", "built"} <= set(df["lifecycle_state"])


def test_rows_without_any_identity_are_dropped():
    assert _identified({"Queue ID": "123", "Project Name": None})
    assert _identified({"Queue ID": None, "Project Name": "Chazy Lake BESS"})
    assert not _identified({"Queue ID": None, "Project Name": None})
    assert not _identified({"Queue ID": float("nan"), "Project Name": "nan"})


def test_every_parsed_row_has_an_identity(parsed):
    _, _, rows, _ = parsed
    assert all(_identified(r) for r in rows)


def test_record_ids_are_unique_after_suffixing(parsed):
    _, _, _, df = parsed
    assert not df["record_id"].duplicated().any()


def test_cross_references_to_other_isos_are_extracted(parsed):
    _, _, _, df = parsed
    assert "cross_refs" in df.columns


def test_provenance(parsed):
    c, _, _, df = parsed
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["source_url"]) == {URL}
    assert set(df["licence_id"]) == {c.source.licence_id}
