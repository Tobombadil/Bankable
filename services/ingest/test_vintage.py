"""A fetch date may never stand in for a release, and "the source states none" is an answer."""

from __future__ import annotations

import pandas as pd
import pytest

from services.ingest import vintage as v


def test_eia_860m_workbook_url_yields_the_report_month() -> None:
    """The exact case that started this: the file loaded on 2026-09-13 was EIA's July report."""
    got = v.from_artefact_url("https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx")
    assert got.value == "2026-07"
    assert got.label == "July 2026"
    assert got.basis == v.ARTEFACT_FILENAME
    assert got.stated is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx",
        "https://www.ercot.com/misapp/GetReports.do?reportTypeId=15933",
        "",
        None,
    ],
)
def test_a_url_naming_no_release_is_not_stated_and_never_a_guess(url: str | None) -> None:
    got = v.from_artefact_url(url)
    assert got.value is None
    assert got.basis == v.NOT_STATED
    assert got.stated is False


@pytest.mark.parametrize(
    ("token", "value", "label"),
    [
        ("202001", "2020-01", "January 2020"),
        ("202012", "2020-12", "December 2020"),
        ("2017_v2", "2017", "2017"),
        ("2017", "2017", "2017"),
    ],
)
def test_atlas_shapefile_tokens_normalise(token: str, value: str, label: str) -> None:
    got = v.from_atlas_token(token)
    assert (got.value, got.label, got.basis) == (value, label, v.SHAPEFILE_MEMBER)


def test_an_unrecognised_token_is_kept_verbatim_rather_than_reshaped() -> None:
    """Inventing precision is the failure this module exists to prevent, so a token in no known
    shape is published as the source spelled it."""
    got = v.from_atlas_token("spring-2019")
    assert got.value == "spring-2019"
    assert got.label == "spring-2019"
    assert got.basis == v.SHAPEFILE_MEMBER


def test_blank_or_missing_tokens_are_not_stated() -> None:
    assert v.from_atlas_token(None).basis == v.NOT_STATED
    assert v.from_atlas_token("   ").basis == v.NOT_STATED
    assert v.from_attributes(None).basis == v.NOT_STATED
    assert v.from_attributes({"other": 1}).basis == v.NOT_STATED
    assert v.from_attributes({"source_vintage": "202004"}).value == "2020-04"


def test_a_frame_of_urls_answers_from_the_first_that_names_a_release() -> None:
    urls = pd.Series([None, "", "https://www.eia.gov/electricity/data/eia860m/xls/march_generator2025.xlsx"])
    assert v.from_source_urls(urls).value == "2025-03"


def test_a_frame_of_urls_naming_none_is_not_stated_and_stops_scanning() -> None:
    urls = pd.Series(["https://example.org/queue.xlsx"] * 5000)
    got = v.from_source_urls(urls)
    assert got.basis == v.NOT_STATED


def test_values_sort_as_releases_without_date_parsing() -> None:
    """`min()` over the column is how "oldest loaded release" is answered, so text order has to
    be release order across both shapes."""
    values = ["2026-07", "2017", "2020-01", "2020-12"]
    assert min(values) == "2017"
    assert sorted(values) == ["2017", "2020-01", "2020-12", "2026-07"]


def test_labels_leave_a_year_only_value_alone() -> None:
    assert v.label_for("2017") == "2017"
    assert v.label_for(None) is None
    assert v.label_for("2026-13") == "2026-13"  # not a month; not ours to reformat


def test_the_module_offers_no_way_to_derive_a_vintage_from_a_fetch_date() -> None:
    """A guard, not a tautology: every future extractor must take something the source published.
    If a `from_retrieved_at` ever appears, this fails and the reviewer has to argue for it."""
    exported = {name for name in dir(v) if name.startswith("from_")}
    assert exported == {"from_artefact_url", "from_atlas_token", "from_attributes", "from_source_urls"}


# ------------------------------------------------------- the loader writes it on every load
def _session():
    from services.db.session import get_engine, get_sessionmaker, init_db

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)()


def _load(source_url: str | None):
    from services.ingest.loader import load_dataframe, upsert_licence_and_source
    from services.ingest.test_loader import open_source_entry, sample_proposal_row

    session = _session()
    source = upsert_licence_and_source(session, open_source_entry(), "test")
    row = sample_proposal_row()
    if source_url is None:
        frame = pd.DataFrame([{k: v for k, v in row.items() if k != "source_url"}])
    else:
        frame = pd.DataFrame([{**row, "source_url": source_url}])
    load_dataframe(session, source, "proposal", frame, None)
    session.flush()
    return source


def test_a_load_records_the_release_the_artefact_states() -> None:
    source = _load("https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx")
    assert source.vintage == "2026-07"
    assert source.vintage_basis == v.ARTEFACT_FILENAME


def test_a_load_records_not_stated_rather_than_leaving_the_column_empty() -> None:
    """ "Examined, states none" is an answer the surfaces render; NULL would be indistinguishable
    from "never examined"."""
    source = _load("https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx")
    assert source.vintage is None
    assert source.vintage_basis == v.NOT_STATED


def test_a_frame_with_no_source_url_column_is_not_stated_not_the_fetch_date() -> None:
    source = _load(None)
    assert source.vintage is None
    assert source.vintage_basis == v.NOT_STATED
