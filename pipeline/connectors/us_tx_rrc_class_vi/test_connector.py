"""Tests for us.tx.rrc.class_vi on synthetic input: no RRC document is committed.

The source is `reuse: unknown` and the owner tabled its commercial use on 2026-09-25
(`docs/00-PLAN.md` decisions log), so the two list PDFs recorded that day were removed from
`tests/fixtures/`. What is pinned here instead:

* the post-`extract_tables` stage, `rows_from_tables()`, on hand-built cell arrays that reproduce
  every layout seen — the 2026-09-22 build (title and `Last Updated:` rows above the header,
  footnote legend below; transcribed cell for cell with pdfplumber's line breaks), the 2026-03-11
  build (header first, `Last Updated:` trailer row inside the table, the earlier header spelling),
  the hyphen line-break join, the footnote-marker strip, the 14-cell invariant, a header repeated
  on a later page and the mis-alignment signature;
* the 20-row release's *parsed content* as `EXPECTED_ROWS`, written by hand from the parse made on
  2026-09-25 — facts about applications, not the document;
* `normalize()` over those rows, the landing-page discovery over a scripted page, and the
  publication gate with a real `run()` routed to quarantine.

One end-to-end test reads a real PDF from the gitignored `tests/fixtures/local/` and skips when
none is there, so a developer with the file can run it and CI never needs it.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
from typing import Any, ClassVar

import pytest

from conftest import connector_for
from pipeline.connectors.base import ConnectorError, GateViolation, ParseError, RawSnapshot
from pipeline.connectors.canonical import classify_tech
from pipeline.connectors.registry import GATED_REUSE
from pipeline.connectors.us_tx_rrc_class_vi.connector import (
    COLUMN_KEYS,
    PAGE_URL,
    Candidate,
    choose_current,
    clean_status,
    find_list_links,
    rows_from_tables,
)

SOURCE_ID = "us.tx.rrc.class_vi"
CURRENT_URL = "https://www.rrc.texas.gov/media/3ivkqhtb/class_vi_application-09222026.pdf"
MARCH_URL = "https://www.rrc.texas.gov/media/sutpod2t/class-vi-application-list-31126.pdf"
#: Gitignored (`tests/fixtures/local/`): a developer who holds a release of the list drops it here.
LOCAL_DIR = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "local"
LOCAL_PDFS = sorted(LOCAL_DIR.glob("tx_rrc_class_vi_*.pdf"))

# ------------------------------------------------------------------ the 2026-09-22 build, cell for cell
_BLANK13: list[Any] = [None] * 13
SEPT_TITLE = ["Texas RRC Class VI Application Tracker — Project Summary", *_BLANK13]
SEPT_UPDATED = ["Last Updated: 09/22/2026", *_BLANK13]
SEPT_HEADER = [
    "RRC\nTracking #", "Operator Name", "Project Name", "County", "District", "Formation",
    "No. of\nInj. Wells", "Inj. Interval\n(TVD)", "Submittal\nDate", "Application Status",
    "Protested", "Referred to\nHearing Date", "Docket #", "Approval\nDate",
]  # fmt: skip
SEPT_ROWS: list[list[Any]] = [
    ["55294", "Oxy Low Carbon Ventures, LLC", "Brown Pelican", "Ector", "08", "Lower San Andres", "3", "4,402'-5,177'", "05/23/2022", "Approved", "No", "-", "-", "10/16/2025"],  # noqa: E501
    ["56713", "BP Carbon Sol. LLC", "Jasper County Storage Facility", "Jasper", "03", "Frio", "4", "6,100'-6,740'", "06/29/2023", "On Hold by Operator", "-", "-", "-", "-"],  # noqa: E501
    ["57172", "Pineywoods CCS, LLC (Tenaska)", "Pineywoods CCS Hub", "Liberty, Hardin &\nJefferson", "03", "Frio", "4", "6,000'-7,256'", "10/26/2023", "Withdrawn", "-", "-", "-", "-"],  # noqa: E501
    ["57099", "Milestone Carbon Midland CCS Hub,\nLLC", "South Midland Facility", "Upton", "7C", "Devonian", "1", "12,200'-13,849'", "12/06/2023", "Hearing", "Yes", "4/28/2026", "OG-26-\n00031321", "-"],  # noqa: E501
    ["57103", "BP Carbon Sol. LLC", "West Bay Storage Facility", "Galveston", "03", "Miocene", "3", "5,909'-6,408'", "12/18/2023", "On Hold by Operator", "-", "-", "-", "-"],  # noqa: E501
    ["57802", "North Texas Carbon, LLC", "Texas Carbon Storage I", "Deaf Smith", "10", "Granite Wash", "1", "8,100'-9,400'", "02/15/2024", "Under Review", "-", "-", "-", "-"],  # noqa: E501
    ["57803", "ExxonMobil Low Carbon Sol.\nOnshore LLC", "Rose CCS", "Jefferson", "03", "Fleming, Upper Frio", "3", "3,340'-8,455'", "03/14/2024", "Hearing", "Yes", "10/31/2025", "OG-25-\n00029632", ""],  # noqa: E501
    ["57804", "Titan Carbon Sequestration, LLC\n(Sempra)", "Titan CS", "Jefferson", "03", "Lower Miocene", "2", "5,735'-8,334'", "07/09/2024", "On Hold by Operator", "-", "-", "-", "-"],  # noqa: E501
    ["57988", "BKVerde, LLC", "Whites Bayou", "Liberty", "03", "Frio, Miocene", "1", "3,825'-7,520'", "07/19/2024", "Pending RAD\nResponse", "-", "-", "-", "-"],  # noqa: E501
    ["57900", "Bluebonnet Sequestration Hub, LLC\n(Oxy)", "Bluebonnet", "Chambers &\nLiberty", "03", "Frio, Hackberry", "6", "7,346'-8,286'", "08/06/2024", "Draft Doc6", "Yes", "-", "-", "-"],  # noqa: E501
    ["58219", "CDP II CO2 Sequestration, LLC", "Caliche Beaumont Sequestration", "Jefferson", "03", "Frio", "3", "7,200'-7,500'", "11/13/2024", "On Hold by Operator", "-", "-", "-", "-"],  # noqa: E501
    ["58220", "Kleberg Sequestration Hub, LLC\n(Oxy)", "South Texas Sequestration (Kleberg\nHub)", "Kleberg", "04", "Frio", "6", "6,302'-9,638'", "11/19/2024", "Under Review", "-", "-", "-", "-"],  # noqa: E501
    ["58339", "Bayou Bend CCS LLC (Chevron,\nTotal, Equinor)", "Bayou Bend East Phase 1 (BBE-P1)", "Jefferson", "03", "Lower Miocene 1 & 2", "6", "5,850’-9,150’", "12/31/2024", "Pending RAD\nResponse", "-", "-", "-", "-"],  # noqa: E501
    ["58608", "ExxonMobil Low Carbon Sol.\nOnshore LLC", "Sunflower", "Jefferson, Liberty", "03", "Frio, Fleming", "3", "4,188'-7,687'", "12/18/2025", "Draft Permit", "-", "-", "-", "-"],  # noqa: E501
    ["58440", "Milestone Carbon Delaware CCS\nHub, LLC", "Central Loving Facility", "Loving", "08", "Devonian, Fusselman,\nEllenburger", "2", "17,945'-22,781'", "02/13/2025", "Withdrawn", "-", "-", "-", "-"],  # noqa: E501
    ["58904", "Sugarberry CCS LLC (Tenaska)", "Sugarberry CCS Geologic Storage\nFacility", "Hopkins & Franklin", "05/06", "Woodbine & Paluxy", "5", "3,560 '-5,366'", "06/06/2025", "Under Review", "-", "-", "-", "-"],  # noqa: E501
    ["58947", "Orchard Storage Company", "Orchard", "Gaines", "8A", "San Andres", "7", "5,230'-6,760", "06/25/2025", "Pending RAD\nResponse", "-", "-", "-", "-"],  # noqa: E501
    ["59020", "Repsol Earth Solutions USA LLC", "Offshore North 1", "Aransas", "04", "Lower Miocene 1 & 2", "4", "4,895' - 6,867'", "08/06/2025", "Pending RAD\nResponse", "-", "-", "-", "-"],  # noqa: E501
    ["59502", "Oxy Low Carbon Ventures, LLC", "Brown Pelican (Amendment)", "Ector", "08", "Lower San Andres", "3", "4,402'-5,177'", "04/01/2026", "Approved", "-", "-", "-", "08/20/2026"],  # noqa: E501
    ["59737", "Bayou Bend CCS LLC (Chevron,\nTotal, Equinor)", "Bayou Bend West Phase 1 (BBW-\nP1)", "Jefferson", "03", "Golliad, Lagarto &\nOakville", "6", "5,197'-9,200'", "06/29/2026", "Under Review", "-", "-", "-", "-"],  # noqa: E501
]  # fmt: skip
SEPT_TABLE: list[list[Any]] = [SEPT_TITLE, SEPT_UPDATED, SEPT_HEADER, *SEPT_ROWS]

#: A representative subset of what the 2026-09-22 build parses to (COLUMN_KEYS order, then
#: application_status_raw), written by hand from the 2026-09-25 parse: one row per distinct status
#: value, the operator-rename case (57802), the amendment case (59502) and the hyphen-join case
#: (59737). Facts about applications, kept to the minimum the parser needs pinned.
EXPECTED_ROWS: list[tuple[Any, ...]] = [
    ("55294", "Oxy Low Carbon Ventures, LLC", "Brown Pelican", "Ector", "08", "Lower San Andres", "3", "4,402'-5,177'", "05/23/2022", "Approved", "No", None, None, "10/16/2025", "Approved"),  # noqa: E501
    ("56713", "BP Carbon Sol. LLC", "Jasper County Storage Facility", "Jasper", "03", "Frio", "4", "6,100'-6,740'", "06/29/2023", "On Hold by Operator", None, None, None, None, "On Hold by Operator"),  # noqa: E501
    ("57099", "Milestone Carbon Midland CCS Hub, LLC", "South Midland Facility", "Upton", "7C", "Devonian", "1", "12,200'-13,849'", "12/06/2023", "Hearing", "Yes", "4/28/2026", "OG-26-00031321", None, "Hearing"),  # noqa: E501
    ("57802", "North Texas Carbon, LLC", "Texas Carbon Storage I", "Deaf Smith", "10", "Granite Wash", "1", "8,100'-9,400'", "02/15/2024", "Under Review", None, None, None, None, "Under Review"),  # noqa: E501
    ("57988", "BKVerde, LLC", "Whites Bayou", "Liberty", "03", "Frio, Miocene", "1", "3,825'-7,520'", "07/19/2024", "Pending RAD Response", None, None, None, None, "Pending RAD Response"),  # noqa: E501
    ("57900", "Bluebonnet Sequestration Hub, LLC (Oxy)", "Bluebonnet", "Chambers & Liberty", "03", "Frio, Hackberry", "6", "7,346'-8,286'", "08/06/2024", "Draft Doc", "Yes", None, None, None, "Draft Doc6"),  # noqa: E501
    ("58608", "ExxonMobil Low Carbon Sol. Onshore LLC", "Sunflower", "Jefferson, Liberty", "03", "Frio, Fleming", "3", "4,188'-7,687'", "12/18/2025", "Draft Permit", None, None, None, None, "Draft Permit"),  # noqa: E501
    ("58440", "Milestone Carbon Delaware CCS Hub, LLC", "Central Loving Facility", "Loving", "08", "Devonian, Fusselman, Ellenburger", "2", "17,945'-22,781'", "02/13/2025", "Withdrawn", None, None, None, None, "Withdrawn"),  # noqa: E501
    ("59502", "Oxy Low Carbon Ventures, LLC", "Brown Pelican (Amendment)", "Ector", "08", "Lower San Andres", "3", "4,402'-5,177'", "04/01/2026", "Approved", None, None, None, "08/20/2026", "Approved"),  # noqa: E501
    ("59737", "Bayou Bend CCS LLC (Chevron, Total, Equinor)", "Bayou Bend West Phase 1 (BBW-P1)", "Jefferson", "03", "Golliad, Lagarto & Oakville", "6", "5,197'-9,200'", "06/29/2026", "Under Review", None, None, None, None, "Under Review"),  # noqa: E501
]  # fmt: skip
EXPECTED_KEYS = (*COLUMN_KEYS, "application_status_raw")

# ------------------------------------------------------------------ the 2026-03-11 build's layout
MARCH_HEADER = [
    "RRC\nTracking #", "Operator Name", "Project Name", "County", "District", "Formation",
    "No . of Inj.\nWell", "Inj. Interval\n(TVD)", "Submittal Date", "Application Status",
    "Protested", "Referred\nto\nHearing\nDate", "Docket #", "Approval Date",
]  # fmt: skip
MARCH_ROWS: list[list[Any]] = [
    ["55294", "Oxy Low Carbon Ventures, LLC", "Brown Pelican", "Ector", "08", "Lower San Andres", "3", "4,402'-5,177'", "05/23/22", "Approved", "No", "-", "-", "10/16/2025"],  # noqa: E501
    ["57099", "Milestone Carbon Midland CCS Hub, LLC", "South Midland Facility", "Upton", "7C", "Devonian", "1", "12,200'-13,849'", "12/06/23", "Comment Period for Draft Pemit", "-", "-", "-", "-"],  # noqa: E501
    ["57802", "White Energy Carbon Sol. LLC", "Texas Carbon Storage I", "Deaf Smith", "10", "Granite Wash", "1", "8,100'-9,400'", "02/15/24", "On Hold by Operator", "-", "-", "-", "-"],  # noqa: E501
    ["59020", "Repsol Earth Solutions USA LLC", "Offshore North 1", "Aransas", "04", "Lower Miocene 1 & 2", "4", "4,895' - 6,867'", "8/6/2025", "Under Review", "-", "-", "-", "-"],  # noqa: E501
]  # fmt: skip
MARCH_TRAILER = ["", "", "", "", "", "", "", "", "", "", "", "", "Last Updated:", "03/11/26"]
MARCH_TABLE: list[list[Any]] = [MARCH_HEADER, *MARCH_ROWS, MARCH_TRAILER]


def _connector(**kwargs):
    return connector_for(SOURCE_ID, allow_restricted=True, **kwargs)


def _rows(table: list[list[Any]]) -> list[dict[str, Any]]:
    rows, last_updated = rows_from_tables([table])
    for r in rows:
        r["list_last_updated"] = last_updated
        r["page_url"] = PAGE_URL
    return rows


def _raw(url: str = CURRENT_URL) -> RawSnapshot:
    return RawSnapshot(
        content=b"%PDF-1.7 synthetic stand-in; parse() is never called on it",
        content_type="application/pdf",
        url=url,
        retrieved_at=dt.datetime(2026, 9, 25, 16, 0, tzinfo=dt.UTC),
        http_status=200,
        ext="pdf",
    )


@pytest.fixture(scope="module")
def current():
    c = _connector()
    raw = _raw()
    rows = _rows(SEPT_TABLE)
    return c, raw, rows, c.normalize(rows, raw)


@pytest.fixture(scope="module")
def march():
    c = _connector()
    raw = _raw(MARCH_URL)
    rows = _rows(MARCH_TABLE)
    return c, raw, rows, c.normalize(rows, raw)


# ------------------------------------------------------------------ rows_from_tables: layouts
def test_the_september_layout_yields_twenty_unique_applications():
    rows, last_updated = rows_from_tables([SEPT_TABLE])
    assert last_updated == "2026-09-22"
    assert len(rows) == 20
    assert len({r["rrc_tracking_no"] for r in rows}) == 20
    assert all(set(r) == set(EXPECTED_KEYS) for r in rows)
    assert len({r["operator_name"] for r in rows}) == 16
    assert {r["application_status"] for r in rows} == {e[9] for e in EXPECTED_ROWS}


def test_the_representative_rows_parse_exactly_as_pinned():
    by_id = {r["rrc_tracking_no"]: r for r in rows_from_tables([SEPT_TABLE])[0]}
    for expected in EXPECTED_ROWS:
        assert tuple(by_id[expected[0]][k] for k in EXPECTED_KEYS) == expected, expected[0]


def test_the_march_layout_finds_the_trailer_date_and_the_earlier_header_spelling():
    """Header first, `Last Updated:` inside the table as a trailer row, `No . of Inj.\\nWell`."""
    rows, last_updated = rows_from_tables([MARCH_TABLE])
    assert last_updated == "2026-03-11"
    assert [r["rrc_tracking_no"] for r in rows] == ["55294", "57099", "57802", "59020"]
    assert rows[0]["injection_wells"] == "3"
    assert rows[1]["application_status"] == "Comment Period for Draft Pemit"  # sic, the RRC's spelling
    assert rows[2]["operator_name"] == "White Energy Carbon Sol. LLC"  # renamed by September
    assert rows[3]["submittal_date"] == "8/6/2025"


def test_multi_line_cells_are_rejoined_and_hyphenation_closed_up():
    by_id = {r["rrc_tracking_no"]: r for r in rows_from_tables([SEPT_TABLE])[0]}
    assert by_id["57803"]["docket_no"] == "OG-25-00029632"
    assert by_id["57099"]["docket_no"] == "OG-26-00031321"
    assert by_id["57099"]["operator_name"] == "Milestone Carbon Midland CCS Hub, LLC"
    assert by_id["59737"]["project_name"] == "Bayou Bend West Phase 1 (BBW-P1)"
    assert by_id["57988"]["application_status"] == "Pending RAD Response"
    assert by_id["57172"]["county"] == "Liberty, Hardin & Jefferson"
    assert by_id["58440"]["formation"] == "Devonian, Fusselman, Ellenburger"


def test_footnote_markers_are_stripped_from_the_mapped_status_and_kept_verbatim():
    by_id = {r["rrc_tracking_no"]: r for r in rows_from_tables([SEPT_TABLE])[0]}
    assert by_id["57900"]["application_status_raw"] == "Draft Doc6"
    assert by_id["57900"]["application_status"] == "Draft Doc"
    starred = [list(SEPT_ROWS[8][:9]), "Pending RAD\nResponse**", *SEPT_ROWS[8][10:]]
    starred = [*starred[0], *starred[1:]]
    row = rows_from_tables([[SEPT_HEADER, starred]])[0][0]
    assert row["application_status_raw"] == "Pending RAD Response**"
    assert row["application_status"] == "Pending RAD Response"
    assert clean_status("Under Review") == "Under Review"
    assert clean_status(None) is None


def test_blank_and_dash_cells_are_null():
    by_id = {r["rrc_tracking_no"]: r for r in rows_from_tables([SEPT_TABLE])[0]}
    assert by_id["56713"]["protested"] is None  # "-"
    assert by_id["57803"]["approval_date"] is None  # ""
    assert by_id["55294"]["approval_date"] == "10/16/2025"


def test_a_footnote_legend_row_below_the_table_is_preamble_not_a_row():
    legend = ["Draft Permit: Drafting Permit & Notice Documents for Public Comment Period", *_BLANK13]
    rows, _ = rows_from_tables([[*SEPT_TABLE, legend]])
    assert len(rows) == 20


# ------------------------------------------------------------------ rows_from_tables: drift fails closed
def test_a_column_count_change_is_a_parse_error():
    with pytest.raises(ParseError, match="15 cells"):
        rows_from_tables([[[*SEPT_HEADER, "New Column"], [*SEPT_ROWS[0], "x"]]])
    with pytest.raises(ParseError, match="13 cells"):
        rows_from_tables([[SEPT_HEADER[:-1], SEPT_ROWS[0][:-1]]])


def test_a_renamed_header_is_a_parse_error_not_a_misaligned_column():
    renamed = list(SEPT_HEADER)
    renamed[9] = "Status"  # "Application Status" -> something else
    with pytest.raises(ParseError, match="header row not found"):
        rows_from_tables([[renamed, SEPT_ROWS[0]]])


def test_a_reordered_header_is_a_parse_error():
    swapped = list(SEPT_HEADER)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    with pytest.raises(ParseError, match="header row not found"):
        rows_from_tables([[swapped, SEPT_ROWS[0]]])


def test_a_header_repeated_on_a_later_page_is_skipped_not_parsed_as_a_row():
    page_one = [SEPT_TITLE, SEPT_UPDATED, SEPT_HEADER, *SEPT_ROWS[:10]]
    page_two = [SEPT_HEADER, *SEPT_ROWS[10:]]
    rows, last_updated = rows_from_tables([page_one, page_two])
    assert [r["rrc_tracking_no"] for r in rows] == [r[0] for r in SEPT_ROWS]
    assert last_updated == "2026-09-22"


def test_a_row_without_a_tracking_number_is_a_parse_error():
    shifted = ["", *SEPT_ROWS[6][:-1]]  # everything one column right: the mis-alignment signature
    with pytest.raises(ParseError, match="no tracking number"):
        rows_from_tables([[SEPT_HEADER, shifted]])


def test_a_table_with_no_header_at_all_is_a_parse_error():
    with pytest.raises(ParseError, match="header row not found"):
        rows_from_tables([[SEPT_ROWS[0]]])


def test_blank_rows_are_ignored():
    rows, _ = rows_from_tables([[SEPT_HEADER, [None] * 14, SEPT_ROWS[0], [""] * 14]])
    assert len(rows) == 1


# ------------------------------------------------------------------ parse(): the bytes gate
def test_a_payload_that_is_not_a_pdf_is_a_parse_error():
    raw = _raw()
    raw.content = b"<!doctype html><html><body>Page not found</body></html>"
    with pytest.raises(ParseError):
        _connector().parse(raw)


def test_a_damaged_pdf_is_a_parse_error():
    raw = _raw()
    raw.content = b"%PDF-1.7 truncated"
    with pytest.raises(ParseError):
        _connector().parse(raw)


# ------------------------------------------------------------------ identity
def test_record_id_is_the_rrcs_own_tracking_number(current):
    _, _, rows, df = current
    assert (df["source_record_id"] == [r["rrc_tracking_no"] for r in rows]).all()
    assert (df["queue_id"] == df["source_record_id"]).all()
    assert (df["record_id"] == SOURCE_ID + ":" + df["source_record_id"]).all()
    assert not df["record_id"].duplicated().any()
    assert not df["source_record_id"].str.startswith("h").any()  # no content-hash path needed


def test_tracking_numbers_are_stable_across_releases(current, march):
    """A March row keeps its key in September even when its operator was renamed (57802)."""
    _, _, sept_rows, sept = current
    _, _, march_rows, mar = march
    assert set(mar["record_id"]) <= set(sept["record_id"])
    sept_ops = {r["rrc_tracking_no"]: r["operator_name"] for r in sept_rows}
    march_ops = {r["rrc_tracking_no"]: r["operator_name"] for r in march_rows}
    assert march_ops["57802"] != sept_ops["57802"]
    assert march_ops["55294"] == sept_ops["55294"]


def test_docket_numbers_become_cross_refs(current):
    _, _, rows, df = current
    with_docket = [i for i, r in enumerate(rows) if r["docket_no"]]
    assert len(with_docket) == 2
    for i in with_docket:
        assert df["cross_refs"].iloc[i] == f"RRC_DOCKET:{rows[i]['docket_no']}"
    without = [i for i in range(len(rows)) if i not in with_docket]
    assert (df["cross_refs"].iloc[without] == "").all()


# ------------------------------------------------------------------ lifecycle
def test_every_status_in_both_layouts_is_in_the_map(current, march):
    for _, _, _, df in (current, march):
        assert (df["status_rule"] == "tx_rrc_class_vi.map").all()
        assert set(df["lifecycle_state"]) <= {"studied", "permitted", "withdrawn"}
        assert not df["status_raw"].isna().any()


def test_approved_is_permitted_not_built(current):
    _, _, _, df = current
    approved = df[df["status_raw"] == "Approved"]
    assert len(approved) == 2
    assert set(approved["lifecycle_state"]) == {"permitted"}


def test_the_judgement_calls_land_on_studied(current, march):
    _, _, _, sept = current
    _, _, _, mar = march
    for status in ("On Hold by Operator", "Pending RAD Response", "Hearing", "Draft Doc", "Draft Permit"):
        assert set(sept[sept["status_raw"] == status]["lifecycle_state"]) == {"studied"}, status
    comment = mar[mar["status_raw"] == "Comment Period for Draft Pemit"]
    assert set(comment["lifecycle_state"]) == {"studied"}


def test_withdrawn_is_withdrawn(current):
    _, _, _, df = current
    assert set(df[df["status_raw"] == "Withdrawn"]["lifecycle_state"]) == {"withdrawn"}


def test_an_unknown_status_is_flagged_rather_than_guessed(current):
    c, raw, rows, _ = current
    mutated = [dict(r) for r in rows]
    mutated[0]["application_status"] = "Denied"
    df = c.normalize(mutated, raw)
    assert df["lifecycle_state"].iloc[0] == "unknown"
    assert df["status_rule"].iloc[0] == "tx_rrc_class_vi.unmapped"


# ------------------------------------------------------------------ fields
def test_kind_is_ccs_and_the_battery_storage_misclassification_is_avoided(current):
    """`classify_tech` would call a CO2 storage facility a battery: the connector bypasses it."""
    _, _, _, df = current
    assert set(df["kind"]) == {"ccs"}
    assert set(df["technology"]) == {"co2_geologic_sequestration"}
    assert df["technology_raw"].isna().all()
    assert classify_tech("CO2 geologic storage")[0] == "storage"  # the trap, asserted explicitly


def test_state_is_texas_by_construction_and_county_is_verbatim(current):
    _, _, rows, df = current
    assert set(df["state"]) == {"TX"}
    assert list(df["county"]) == [r["county"] for r in rows]
    multi = df["county_norm"][df["county"] == "Liberty, Hardin & Jefferson"]
    assert multi.iloc[0] == "LIBERTY HARDIN JEFFERSON"


def test_capacity_and_cod_stay_null_and_queue_date_is_the_submittal_date(current):
    _, _, _, df = current
    assert df["capacity_mw"].isna().all()
    assert df["storage_mwh"].isna().all()
    assert df["proposed_cod"].isna().all()
    assert df["queue_date"].notna().all()
    assert str(df["queue_date"].iloc[0].date()) == "2022-05-23"  # 55294, "05/23/2022"


def test_two_digit_years_in_the_march_build_parse_as_this_century(march):
    _, _, rows, df = march
    assert rows[0]["submittal_date"] == "05/23/22"
    assert str(df["queue_date"].iloc[0].date()) == "2022-05-23"
    assert str(df["queue_date"].iloc[3].date()) == "2025-08-06"  # "8/6/2025"


def test_provenance_quartet_and_the_pdf_as_source_url(current):
    c, raw, _, df = current
    assert (df["source_id"] == SOURCE_ID).all()
    assert (df["retrieved_at"] == raw.retrieved_at_iso).all()
    assert (df["licence_id"] == c.source.licence_id).all()
    assert (df["source_url"] == CURRENT_URL).all()


def test_no_column_names_an_individual(current):
    """No table column is a person (docs/13 §5); the raw payload carries the columns and nothing else."""
    person_words = ("contact", "writer", "reviewer", "engineer", "author")
    assert not any(w in k for k in COLUMN_KEYS for w in person_words)
    _, _, _, df = current
    raw_keys = set(EXPECTED_KEYS) | {"list_last_updated", "page_url"}
    assert all(set(json.loads(r)) == raw_keys for r in df["raw"])


# ------------------------------------------------------------------ discovery
PAGE_HTML = (
    '<li><a href="/media/r2ndw5md/class_vi_application-5122026.pdf" '
    'title="Class VI Application 5.12.2026"></a>'
    '<a rel="noopener" href="/media/3ivkqhtb/class_vi_application-09222026.pdf" target="_blank" '
    'title="Class VI Application 09.22.2026">Class VI Application List</a><a id="ClassVI"></a>'
    '<a rel="noopener" href="/media/sutpod2t/class-vi-application-list-31126.pdf" target="_blank" '
    'title="Class VI Application List 3.11.26"></a></li>'
    '<a href="/media/t5eagunt/eir-accessibility-policy-and-procedures-manual.pdf">other pdf</a>'
)


def test_every_application_list_anchor_is_found_with_its_title_date():
    """The real page markup on 2026-09-25: three dated anchors, two with empty link text."""
    links = find_list_links(PAGE_HTML)
    assert [c.date for c in links] == ["2026-05-12", "2026-09-22", "2026-03-11"]
    assert [c.text for c in links] == ["", "Class VI Application List", ""]
    assert links[1].url == CURRENT_URL
    assert links[2].url == MARCH_URL
    assert find_list_links("<p>no list here</p>") == []


def test_the_latest_dated_candidate_wins_whatever_the_page_order():
    chosen, rule = choose_current(find_list_links(PAGE_HTML))
    assert chosen.url == CURRENT_URL
    assert rule == "title_date"


def test_the_file_name_date_is_the_fallback_when_the_title_carries_none():
    html = (
        '<a href="/media/x/class_vi_application-09222026.pdf">Class VI Application List</a>'
        '<a href="/media/y/class-vi-application-list-31126.pdf"></a>'
    )
    links = find_list_links(html)
    assert [c.date for c in links] == ["2026-09-22", "2026-03-11"]


def test_visible_anchor_then_page_order_when_nothing_is_dated():
    undated = [
        Candidate("https://x/a/class_vi_application.pdf", "", "", None),
        Candidate("https://x/b/class_vi_application.pdf", "", "Class VI Application List", None),
    ]
    assert choose_current(undated) == (undated[1], "anchor_text")
    assert choose_current([undated[0]]) == (undated[0], "page_order")
    with pytest.raises(ConnectorError):
        choose_current([])


class _Answer:
    def __init__(self, content: bytes, content_type: str, status: int = 200) -> None:
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers: dict[str, str] = {"Content-Type": content_type}


PDF_STAND_IN = b"%PDF-1.7\n% synthetic bytes: fetch() checks the magic, nothing here is parsed\n"


class _Http:
    """The landing page, then the chosen URL answering PDF bytes."""

    page: ClassVar[str] = PAGE_HTML

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **_: object) -> _Answer:
        self.urls.append(url)
        if url == PAGE_URL:
            return _Answer(self.page.encode(), "text/html")
        if url == CURRENT_URL:
            return _Answer(PDF_STAND_IN, "application/pdf")
        return _Answer(b"<html>not the list</html>", "text/html")


def test_fetch_discovers_the_current_pdf_and_records_every_candidate():
    import hashlib

    http = _Http()
    raw = _connector(http=http).fetch()
    assert http.urls == [PAGE_URL, CURRENT_URL]
    assert raw.url == CURRENT_URL and raw.ext == "pdf" and raw.requests_made == 2
    assert raw.content == PDF_STAND_IN
    assert raw.meta["chosen_url"] == CURRENT_URL and raw.meta["chosen_by"] == "title_date"
    assert raw.meta["pdf_bytes"] == len(PDF_STAND_IN)
    assert raw.meta["pdf_sha256"] == hashlib.sha256(PDF_STAND_IN).hexdigest()
    assert [c["date"] for c in raw.meta["candidates"]] == ["2026-05-12", "2026-09-22", "2026-03-11"]
    assert raw.meta["page_bytes"] == len(PAGE_HTML.encode())
    assert raw.meta["page_sha256"] == hashlib.sha256(PAGE_HTML.encode()).hexdigest()


def test_fetch_fails_closed_when_the_page_no_longer_links_a_list():
    class _Moved(_Http):
        page = "<p>The application list has moved.</p>"

    with pytest.raises(ConnectorError, match="no Class VI application-list"):
        _connector(http=_Moved()).fetch()


def test_fetch_fails_closed_when_the_chosen_link_answers_html():
    class _Html(_Http):
        page = (
            '<a href="/media/z/class_vi_application-12312026.pdf" '
            'title="Class VI Application 12.31.2026">x</a>'
        )

    with pytest.raises(ConnectorError, match="not a PDF"):
        _connector(http=_Html()).fetch()


# ------------------------------------------------------------------ publication gate
def test_the_source_is_gated_and_the_registry_says_so(registry):
    entry = registry.get(SOURCE_ID)
    assert entry.reuse in GATED_REUSE and entry.reuse == "unknown"
    assert entry.publication == "none"
    assert entry.implemented  # a connector exists ...
    states = {row["id"]: row["state"] for row in registry.status()}
    assert states[SOURCE_ID] == "gated"  # ... and the registry still reports the gate, not `implemented`
    with pytest.raises(GateViolation, match="allow_restricted"):
        registry.instantiate(SOURCE_ID)


def test_a_run_with_the_flag_is_quarantined_and_never_publishable(tmp_path, registry, monkeypatch):
    """The whole runner path — parse, normalise, DQ, diff, store — with the synthetic table standing
    in for the PDF stage, which is the only step that needs the document."""
    from pipeline.connectors.runner import run
    from pipeline.connectors.store import Store
    from pipeline.connectors.us_tx_rrc_class_vi import connector as module

    monkeypatch.setattr(module.Connector, "parse", lambda self, raw: _rows(SEPT_TABLE))
    raw = _raw()
    with pytest.raises(GateViolation):
        run(SOURCE_ID, registry=registry, store=Store(tmp_path), raw=raw)
    result = run(SOURCE_ID, registry=registry, store=Store(tmp_path), allow_restricted=True, raw=raw)
    assert result.status == "ok", result.run["error"]
    assert result.run["publishable"] is False
    assert result.run["rows_fetched"] == 20
    assert result.paths and all("quarantine" in str(p) for p in result.paths.values())
    assert not (tmp_path / "normalized").exists()
    assert not (tmp_path / "events").exists()


# ------------------------------------------------------------------ end to end, developer-local only
@pytest.mark.skipif(not LOCAL_PDFS, reason=f"no RRC list PDF under {LOCAL_DIR} (gitignored, never committed)")
def test_a_real_release_parses_end_to_end_when_a_developer_has_one():
    """Runs only where a real `tx_rrc_class_vi_*.pdf` sits in the gitignored local directory."""
    import pdfplumber

    c = _connector()
    for path in LOCAL_PDFS:
        raw = RawSnapshot.from_file(path, CURRENT_URL, content_type="application/pdf")
        rows = c.parse(raw)
        df = c.normalize(rows, raw)
        assert len(rows) >= 18
        assert all(r["rrc_tracking_no"].isdigit() for r in rows)
        assert all(r["list_last_updated"] for r in rows)
        assert (df["status_rule"] == "tx_rrc_class_vi.map").all(), sorted(set(df["status_raw"]))
        assert not df["record_id"].duplicated().any()
        assert set(df["kind"]) == {"ccs"}
        # The PDF's document metadata names its RRC author; nothing from it may reach a record.
        with pdfplumber.open(io.BytesIO(raw.content)) as pdf:
            metadata = {k: v for k, v in (pdf.metadata or {}).items() if isinstance(v, str) and v.strip()}
        blob = json.dumps(rows) + "".join(df["raw"])
        for key, value in metadata.items():
            assert value not in blob, key
