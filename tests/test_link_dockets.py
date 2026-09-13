"""Docket linkage: pipeline/link_dockets.py. Synthetic in-memory frames, no disk I/O."""

import datetime as dt
import pathlib
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pipeline.backfill_ferc import _link_stats_json, _year_windows, precision_sample
from pipeline.link_dockets import (
    ACTIVE_STATES,
    LINK_COLUMNS,
    _distinctive,
    _explicit_matches,
    _filer_index,
    _sponsor_matches,
    active_link_stats,
    dedupe_filings_per_docket,
    docket_year,
    filter_er_docket_years,
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


# --- Sprint 3 item 5: FERC filer-name backfill --------------------------------------------


def test_docket_year_reads_the_embedded_two_digit_year():
    assert docket_year("ER26-1234-000") == 2026
    assert docket_year("CP24-9-000|CP24-9-001") == 2024
    assert docket_year(None) is None
    assert docket_year("") is None
    assert docket_year("not-a-docket") is None


def test_filter_er_docket_years_keeps_only_er_within_range():
    docs = docs_df(
        [
            ("ferc:1", "t", None, "f", "a1", "ER24-1-000"),
            ("ferc:2", "t", None, "f", "a2", "ER25-1-000"),
            ("ferc:3", "t", None, "f", "a3", "ER26-1-000"),
            ("ferc:4", "t", None, "f", "a4", "CP25-1-000"),  # right year, wrong class
        ]
    )
    kept = filter_er_docket_years(docs, 2024, 2025)
    assert set(kept["record_id"]) == {"ferc:1", "ferc:2"}


def test_dedupe_filings_per_docket_keeps_one_per_filer_and_docket():
    docs = docs_df(
        [
            ("ferc:1", "Order re Acme Solar.", None, "Acme Solar Developers LLC", "a1", "ER26-1-000"),
            # same filer, same primary docket (a later filing under the same docket) -> dropped
            ("ferc:2", "Compliance filing.", None, "Acme Solar Developers LLC", "a2", "ER26-1-001"),
            # same filer, different docket -> kept
            ("ferc:3", "Different project.", None, "Acme Solar Developers LLC", "a3", "ER26-2-000"),
            # different filer, same docket number text coincidentally -> kept
            ("ferc:4", "Another filer.", None, "Beta Wind Partners LLC", "a4", "ER26-1-000"),
        ]
    )
    kept = dedupe_filings_per_docket(docs)
    assert set(kept["record_id"]) == {"ferc:1", "ferc:3", "ferc:4"}


def test_filer_index_collapses_repeated_filers_into_one_row():
    docs = docs_df(
        [
            ("ferc:1", "t", None, "Acme Solar Developers LLC", "a1", "ER26-1-000"),
            ("ferc:2", "t", None, "Acme Solar Developers LLC", "a2", "ER26-2-000"),
            ("ferc:3", "t", None, "Beta Wind Partners LLC", "a3", "ER26-3-000"),
        ]
    )
    idx = _filer_index(docs)
    assert len(idx) == 2
    row = idx[idx["filer_key"] == _distinctive("Acme Solar Developers LLC")].iloc[0]
    assert sorted(row["record_ids"]) == ["ferc:1", "ferc:2"]


def test_sponsor_fuzzy_dedupes_repeated_filings_under_the_same_docket():
    """Two filings from the same filer under the same docket (a compliance filing following the
    original) must not double the sponsor-fuzzy evidence for that one project."""
    iso = iso_df(
        [
            (
                "ercot:1",
                "us.iso.ercot.gen_queue",
                "ERCOT",
                None,
                None,
                None,
                "Acme Solar Developers",
                None,
                "contracted",
            ),
        ]
    )
    docs = docs_df(
        [
            ("ferc:1", "Order re Acme Solar.", None, "Acme Solar Developers LLC", "a1", "ER26-1-000"),
            ("ferc:2", "Compliance filing.", None, "Acme Solar Developers LLC", "a2", "ER26-1-001"),
        ]
    )
    hits = _sponsor_matches(iso, docs, threshold=85.0)
    assert len(hits) == 1
    assert hits[0]["ferc_record_id"] == "ferc:1"


def test_sponsor_fuzzy_still_links_every_distinct_docket_from_a_repeat_filer():
    """Deduping is per (filer, docket) — a genuinely different project from the same sponsor
    across years must still produce its own link."""
    iso = iso_df(
        [
            (
                "ercot:1",
                "us.iso.ercot.gen_queue",
                "ERCOT",
                None,
                None,
                None,
                "Acme Solar Developers",
                None,
                "contracted",
            ),
        ]
    )
    docs = docs_df(
        [
            ("ferc:1", "Order re Acme Solar I.", None, "Acme Solar Developers LLC", "a1", "ER24-1-000"),
            ("ferc:2", "Order re Acme Solar II.", None, "Acme Solar Developers LLC", "a2", "ER26-2-000"),
        ]
    )
    hits = _sponsor_matches(iso, docs, threshold=85.0)
    assert {h["ferc_record_id"] for h in hits} == {"ferc:1", "ferc:2"}


def test_run_links_across_a_multi_year_frame():
    """End-to-end `run()` over a frame spanning three filing years (the multi-year backfill
    path) — one link per year, plus a duplicate-docket filing that must not add a second link."""
    iso = iso_df(
        [
            (
                "ercot:1",
                "us.iso.ercot.gen_queue",
                "ERCOT",
                None,
                None,
                None,
                "Delta Ridge Power LLC",
                None,
                "filed",
            ),
            (
                "caiso:1",
                "us.iso.caiso.gen_queue",
                "CAISO",
                None,
                None,
                None,
                "Epsilon Bay Energy LLC",
                None,
                "studied",
            ),
            (
                "nyiso:1",
                "us.iso.nyiso.gen_queue",
                "NYISO",
                None,
                None,
                None,
                "Zeta Canyon Storage LLC",
                None,
                "under_construction",
            ),
        ]
    )
    docs = docs_df(
        [
            ("ferc:1", "Order re Delta Ridge.", None, "Delta Ridge Power LLC", "a1", "ER24-1-000"),
            ("ferc:2", "Order re Epsilon Bay.", None, "Epsilon Bay Energy LLC", "a2", "ER25-1-000"),
            ("ferc:3", "Order re Zeta Canyon.", None, "Zeta Canyon Storage LLC", "a3", "ER26-1-000"),
            # compliance refiling of ferc:3's docket a year later by the same filer -> deduped
            ("ferc:4", "Compliance filing.", None, "Zeta Canyon Storage LLC", "a4", "ER26-1-001"),
        ]
    )
    links = run(docs, iso, threshold=85.0)
    assert set(links["record_id"]) == {"ercot:1", "caiso:1", "nyiso:1"}
    assert set(links["ferc_record_id"]) == {"ferc:1", "ferc:2", "ferc:3"}
    stats = active_link_stats(iso, links)
    assert stats["n_active"] == 3
    assert stats["n_linked"] == 3


# --- pipeline/backfill_ferc.py: measurement function, fixture-only (no network) --------------


def test_year_windows_are_trailing_365_day_windows_most_recent_first():
    today = dt.date(2026, 9, 13)
    windows = _year_windows(3, today)
    assert windows == [
        (today - dt.timedelta(days=365), today),
        (today - dt.timedelta(days=730), today - dt.timedelta(days=365)),
        (today - dt.timedelta(days=1095), today - dt.timedelta(days=730)),
    ]
    # contiguous, most-recent-first, each exactly 365 days wide
    assert all((e - s).days == 365 for s, e in windows)
    assert all(windows[i][0] == windows[i + 1][1] for i in range(len(windows) - 1))


def test_link_stats_json_has_the_expected_keys_and_rates():
    iso = iso_df(
        [
            ("a:1", "src_a", "X", None, None, None, None, None, "filed"),
            ("a:2", "src_a", "X", None, None, None, None, None, "contracted"),
            ("a:3", "src_a", "X", None, None, None, None, None, "built"),
        ]
    )
    links = pd.DataFrame({"record_id": ["a:1", "a:2"]})
    stats = _link_stats_json(iso, links)
    assert set(stats) == {"n_active", "n_linked", "rate_pct", "per_source"}
    assert stats["n_active"] == 2
    assert stats["n_linked"] == 2
    assert stats["rate_pct"] == pytest.approx(100.0)
    assert stats["per_source"] == [{"source_id": "src_a", "linked": 2, "total": 2, "rate_%": 100.0}]


def test_link_stats_json_scopes_to_the_given_states():
    """The `contracted`/`under_construction`-only bar: passing `states` restricts the active set
    before `active_link_stats` runs, so a `filed` record never counts even though it is active."""
    iso = iso_df(
        [
            ("a:1", "src_a", "X", None, None, None, None, None, "filed"),
            ("a:2", "src_a", "X", None, None, None, None, None, "contracted"),
        ]
    )
    links = pd.DataFrame({"record_id": ["a:1", "a:2"]})
    stats = _link_stats_json(iso, links, states={"contracted", "under_construction"})
    assert stats["n_active"] == 1
    assert stats["n_linked"] == 1


def test_precision_sample_caps_at_sample_size_and_leaves_a_verdict_field():
    links = pd.DataFrame(
        {
            "record_id": [f"iso:{i}" for i in range(5)],
            "ferc_record_id": [f"ferc:{i}" for i in range(5)],
            "method": ["sponsor_fuzzy"] * 5,
            "score": [90.0] * 5,
            "rationale": ["r"] * 5,
        }
    )
    sample = precision_sample(links, sample_size=3, seed=1)
    assert len(sample) == 3
    assert all(row["verdict"] == "unreviewed" for row in sample)
    assert set(sample[0]) >= {"record_id", "ferc_record_id", "method", "score", "rationale", "verdict"}


def test_precision_sample_is_deterministic_given_a_seed():
    links = pd.DataFrame(
        {
            "record_id": [f"iso:{i}" for i in range(20)],
            "ferc_record_id": [f"ferc:{i}" for i in range(20)],
            "method": ["sponsor_fuzzy"] * 20,
            "score": [90.0] * 20,
            "rationale": ["r"] * 20,
        }
    )
    a = precision_sample(links, sample_size=5, seed=42)
    b = precision_sample(links, sample_size=5, seed=42)
    assert [r["record_id"] for r in a] == [r["record_id"] for r in b]


def test_precision_sample_returns_everything_when_fewer_links_than_sample_size():
    links = pd.DataFrame(
        {
            "record_id": ["iso:1"],
            "ferc_record_id": ["ferc:1"],
            "method": ["name_or_queue_id"],
            "score": [100.0],
            "rationale": ["x"],
        }
    )
    assert len(precision_sample(links, sample_size=50, seed=1)) == 1


def test_precision_sample_of_empty_links_is_empty():
    assert precision_sample(pd.DataFrame(columns=LINK_COLUMNS), sample_size=50, seed=1) == []
