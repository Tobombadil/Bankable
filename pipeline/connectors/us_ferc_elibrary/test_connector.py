"""Parser tests for us.ferc.elibrary against recorded eLibrary AdvancedSearch responses."""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.us_ferc_elibrary.connector import _doc_type, _project_name_hint, _state_hint

SOURCE_ID = "us.ferc.elibrary"
URL = "https://elibrary.ferc.gov/eLibrarywebapi/api/Search/AdvancedSearch"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ferc_elibrary_search.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_document_fields_are_the_docs21_ones(parsed):
    c, _, _, df = parsed
    assert c.kind == "document"
    for col in ("title", "doc_type", "published_date", "accession_number", "docket_refs", "identifiers"):
        assert col in df.columns
    assert "capacity_mw" not in df.columns or df["capacity_mw"].isna().all()
    assert "lifecycle_state" not in df.columns or set(df["lifecycle_state"]) == {"filed"}


def test_only_hits_within_the_window_and_docket_class_survive(parsed):
    """23 unique accessions in the fixture; 6 fall outside the 30-day window (old 2003-2018
    "interconnection agreement"/"large generator" description hits) and 0 lack an ER/CP docket."""
    _, _, rows, df = parsed
    assert len(rows) == 17
    for row in rows:
        classes = {m.group() for d in row["docketNumbers"] if (m := re.match(r"[A-Z]+", d))}
        assert classes & {"ER", "CP"}

    def _mdy(s: str) -> dt.date:
        m, day, y = (int(p) for p in s.split("/"))
        return dt.date(y, m, day)

    filed = [_mdy(r["filedDate"]) for r in rows]
    cutoff = dt.date(2026, 9, 12) - dt.timedelta(days=30)
    assert all(cutoff <= d <= dt.date(2026, 9, 12) for d in filed)
    # the old 2003 "Order conditionally accepting Interconnection Service Agreement" hit must be gone
    assert "20030207-3057" not in df["accession_number"].tolist()


def test_deduplicated_across_queries(parsed):
    """20260908-5211 and 20260908-5290 both cite docket ER26-3265; the CP26-9 order and the CP26-9
    certificate hit are different accessions under the same docket and both survive, but an
    accession seen from two different search queries is kept once."""
    _, _, _, df = parsed
    assert df["record_id"].is_unique
    assert df["accession_number"].is_unique


def test_docket_refs_and_identifiers_are_populated(parsed):
    _, _, _rows, df = parsed
    row = df[df["accession_number"] == "20260910-3037"].iloc[0]
    assert row["docket_refs"] == "CP26-9-000|CP26-9-001"
    ids = json.loads(row["identifiers"])
    assert ids["docket_numbers"] == ["CP26-9-000", "CP26-9-001"]
    assert ids["accession_number"] == "20260910-3037"


def test_source_url_is_the_stable_filelist_pattern(parsed):
    _, _, _, df = parsed
    row = df[df["accession_number"] == "20260911-5219"].iloc[0]
    assert row["source_url"] == "https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20260911-5219"


def test_project_name_hint_lifts_a_named_project():
    text = "Comments ... re the NKY Gate Enhancement Project under CP26-19."
    assert _project_name_hint(text) == "NKY Gate Enhancement Project"


def test_project_name_hint_is_none_for_a_plain_tariff_filing():
    text = "Puget Sound Energy, Inc. submits tariff filing per 35.13(a)(2)(iii under ER26-3760"
    assert _project_name_hint(text) is None


def test_state_hint_reads_a_trailing_abbreviation():
    assert _state_hint("a facility in Anytown, TX under CP26-1") == "TX"
    assert _state_hint("no state mentioned here") is None


def test_doc_type_maps_category_and_notice_class():
    assert _doc_type("Issuance", "Order/Opinion") == "order"
    assert _doc_type("Submittal", "Application/Petition/Request") == "filing"
    assert _doc_type("Submittal", "Notice") == "notice"


def test_an_error_body_with_http_200_fails_closed():
    """docs/02 §7: FERC's backend returns HTTP 200 with success:false; treat as error. This is a
    real captured response — sending sortBy anything other than "" 500s internally (module
    docstring finding 2)."""
    c = connector_for(SOURCE_ID)
    raw = snapshot("ferc_elibrary_search_error.json", URL, "application/json")
    with pytest.raises(ParseError):
        c.parse(raw)
