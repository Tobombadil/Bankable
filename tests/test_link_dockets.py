"""Docket linkage: pipeline/link_dockets.py. Synthetic in-memory frames, no disk I/O."""

import pathlib
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pipeline.link_dockets import (
    ACTIVE_STATES,
    LINK_COLUMNS,
    _distinctive,
    _explicit_matches,
    _sponsor_matches,
    active_link_stats,
    run,
)


def iso_df(rows):
    cols = [
        "record_id",
        "source_id",
        "iso",
        "queue_id",
        "name_canonical",
        "name_norm",
        "sponsor_name",
        "sponsor_norm",
        "lifecycle_state",
    ]
    return pd.DataFrame(rows, columns=cols)


def docs_df(rows):
    cols = ["record_id", "title", "project_name_hint", "filer", "accession_number", "docket_refs"]
    return pd.DataFrame(rows, columns=cols)


def test_distinctive_strips_generic_words_and_states():
    assert _distinctive("Central Hudson Gas & Electric Corporation") == "CENTRAL HUDSON"
    assert _distinctive("New York Power Authority") == ""
    assert _distinctive("NextEra Energy Interconnection Holdings, LLC") == "NEXTERA"
    assert _distinctive("PJM Interconnection, L.L.C.") == "PJM"


def test_explicit_match_requires_a_two_word_canonical_name():
    iso = iso_df(
        [
            (
                "ercot:1",
                "us.iso.ercot.gen_queue",
                "ERCOT",
                "15INR0064b",
                "Golden Fields Solar",
                "GOLDEN",
                None,
                None,
                "contracted",
            ),
            ("caiso:1", "us.iso.caiso.gen_queue", "CAISO", "636", "Kansas", "KANSAS", None, None, "studied"),
        ]
    )
    docs = docs_df(
        [
            (
                "ferc:1",
                "Letter order accepting Golden Fields Solar VI, LLC's filing under ER26-1.",
                None,
                "Golden Fields Solar VI, LLC",
                "20260101-0001",
                "ER26-1-000",
            ),
            (
                "ferc:2",
                "Southwest Power Pool submits a Kansas tariff filing under ER26-2.",
                None,
                "Southwest Power Pool, Inc.",
                "20260102-0002",
                "ER26-2-000",
            ),
        ]
    )
    hits = _explicit_matches(iso, docs)
    assert {(h["record_id"], h["ferc_record_id"]) for h in hits} == {("ercot:1", "ferc:1")}
    # "Kansas" alone (one word) must not match, even though it appears verbatim in ferc:2's text


def test_explicit_match_rejects_a_bare_numeric_queue_id():
    """CAISO's queue ids are small bare integers; a 2-3 digit number is a false-positive
    machine against docket numbers, dollar figures and dates in filing text."""
    iso = iso_df(
        [
            ("caiso:22", "us.iso.caiso.gen_queue", "CAISO", "22", None, None, None, None, "studied"),
        ]
    )
    docs = docs_df(
        [
            (
                "ferc:1",
                "Some filing mentioning the number 22 under ER26-22-000 filed for $2,200,000.",
                None,
                "Some Filer LLC",
                "20260101-0001",
                "ER26-22-000",
            ),
        ]
    )
    assert _explicit_matches(iso, docs) == []


def test_explicit_match_accepts_an_alphanumeric_queue_id():
    iso = iso_df(
        [
            ("ercot:1", "us.iso.ercot.gen_queue", "ERCOT", "15INR0064b", None, None, None, None, "filed"),
        ]
    )
    docs = docs_df(
        [
            (
                "ferc:1",
                "A filing that cites queue position 15INR0064b explicitly.",
                None,
                "Some Filer LLC",
                "20260101-0001",
                "ER26-1-000",
            ),
        ]
    )
    hits = _explicit_matches(iso, docs)
    assert len(hits) == 1
    assert hits[0]["rationale"].startswith("queue_id")


def test_sponsor_fuzzy_keeps_a_real_match_and_drops_boilerplate_overlap():
    iso = iso_df(
        [
            (
                "nyiso:1",
                "us.iso.nyiso.gen_queue",
                "NYISO",
                None,
                None,
                None,
                "Central Hudson Gas & Electric",
                None,
                "built",
            ),
            (
                "nyiso:2",
                "us.iso.nyiso.gen_queue",
                "NYISO",
                None,
                None,
                None,
                "New York Power Authority",
                None,
                "built",
            ),
            (
                "nyiso:3",
                "us.iso.nyiso.gen_queue",
                "NYISO",
                None,
                None,
                None,
                "NRG Energy, Inc.",
                None,
                "built",
            ),
        ]
    )
    docs = docs_df(
        [
            (
                "ferc:1",
                "Central Hudson 205 formula rate revisions.",
                None,
                "Central Hudson Gas & Electric Corporation",
                "20260101-0001",
                "ER26-1-000",
            ),
            (
                "ferc:2",
                "New York Independent System Operator compliance filing.",
                None,
                "New York Independent System Operator, Inc.",
                "20260101-0002",
                "ER26-2-000",
            ),
            (
                "ferc:3",
                "Puget Sound Energy tariff refiling.",
                None,
                "Puget Sound Energy, Inc.",
                "20260101-0003",
                "ER26-3-000",
            ),
        ]
    )
    hits = _sponsor_matches(iso, docs, threshold=85.0)
    pairs = {(h["record_id"], h["ferc_record_id"]) for h in hits}
    assert pairs == {("nyiso:1", "ferc:1")}


def test_sponsor_fuzzy_is_restricted_to_er_dockets():
    iso = iso_df(
        [
            (
                "nyiso:1",
                "us.iso.nyiso.gen_queue",
                "NYISO",
                None,
                None,
                None,
                "Central Hudson Gas & Electric",
                None,
                "built",
            ),
        ]
    )
    docs = docs_df(
        [
            (
                "ferc:1",
                "A gas certificate filing.",
                None,
                "Central Hudson Gas & Electric Corporation",
                "20260101-0001",
                "CP26-1-000",
            ),
        ]
    )
    assert _sponsor_matches(iso, docs, threshold=85.0) == []


def test_run_folds_a_pair_found_by_both_methods_into_one_row():
    iso = iso_df(
        [
            (
                "nyiso:1",
                "us.iso.nyiso.gen_queue",
                "NYISO",
                None,
                "Central Hudson Gas Electric",
                "CENTRAL HUDSON",
                "Central Hudson Gas & Electric",
                None,
                "built",
            ),
        ]
    )
    docs = docs_df(
        [
            (
                "ferc:1",
                "Order re Central Hudson Gas Electric filing under ER26-1.",
                None,
                "Central Hudson Gas & Electric Corporation",
                "20260101-0001",
                "ER26-1-000",
            ),
        ]
    )
    links = run(docs, iso, threshold=85.0)
    assert len(links) == 1
    assert set(links["method"].iloc[0].split("+")) == {"name_or_queue_id", "sponsor_fuzzy"}
    assert list(links.columns) == LINK_COLUMNS


def test_run_returns_empty_frame_with_no_data():
    empty_iso = iso_df([])
    empty_docs = docs_df([])
    links = run(empty_docs, empty_iso, threshold=85.0)
    assert links.empty
    assert list(links.columns) == LINK_COLUMNS


def test_active_link_stats_counts_only_active_states():
    iso = iso_df(
        [
            ("a:1", "src_a", "X", None, None, None, None, None, "filed"),  # active
            ("a:2", "src_a", "X", None, None, None, None, None, "built"),  # not active
            ("a:3", "src_a", "X", None, None, None, None, None, "withdrawn"),  # not active
        ]
    )
    links = pd.DataFrame({"record_id": ["a:1", "a:2"]})
    stats = active_link_stats(iso, links)
    assert stats["n_active"] == 1
    assert stats["n_linked"] == 1  # a:2's link doesn't count; it isn't active
    assert stats["rate_pct"] == pytest.approx(100.0)


def test_active_states_match_resolve_py():
    from pipeline.resolve import ACTIVE_STATES as RESOLVE_ACTIVE_STATES

    assert ACTIVE_STATES == RESOLVE_ACTIVE_STATES
