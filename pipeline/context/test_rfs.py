"""Tests for `pipeline.context.rfs`, including its self-contained legacy-.xls reader.

`fixtures/rfs_part80_registrations_sample.xls` holds 24 rows taken verbatim from EPA's real
`Part80FuelsProgramsList` workbook (fetched 2026-09-19, 5,765,120 bytes, sha256 76183ab0…):
ethanol producers carrying D6 and D3, RNG/biogas facilities carrying D3, single-code D5 and D4
rows, registrations with no D code at all, and the second sheet (`RNG RIN Separator Information`)
that the feature set deliberately ignores. The fixture is a real OLE2/BIFF8 workbook -- the rows
were repackaged with a one-off generator because LibreOffice is not usable in this environment --
and its bytes were checked against an independent reader (xlrd) before being committed: 25 rows x
23 columns, zero cell differences.

No network: `fetch` is not exercised here, only `parse`/`build_rows` and the reader.
"""

from __future__ import annotations

import json
import pathlib
import struct

import pytest

from pipeline.connectors.base import ParseError
from pipeline.context import rfs

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "rfs_part80_registrations_sample.xls"


@pytest.fixture(scope="module")
def raw():
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def frame(raw):
    return rfs.parse(raw)


@pytest.fixture(scope="module")
def rows(frame):
    return rfs.build_rows(
        frame, retrieved_at="2026-09-19T21:52:09Z", licence_id="us.epa.rfs_public_data#test"
    )


# ------------------------------------------------------------------------------- the .xls reader
def test_reader_opens_the_compound_file_and_finds_both_sheets(raw):
    streams = rfs._ole_streams(raw)
    assert "Workbook" in streams
    table = rfs.read_xls_sheet(raw, rfs.COMPANY_SHEET)
    assert len(table) == 25  # header + 24 registrations
    assert table[0][:2] == ["Company ID", "Company Name"]
    # The separator sheet is present in the file and addressable, but unused by the feature set.
    assert rfs.read_xls_sheet(raw, "RNG RIN Separator Information")[0][0] == "Company ID"


def test_reader_rejects_bytes_that_are_not_a_compound_file():
    with pytest.raises(ParseError):
        rfs.read_xls_sheet(b"PK\x03\x04 this is a zip, not an xls", rfs.COMPANY_SHEET)


def test_reader_rejects_an_unknown_sheet(raw):
    with pytest.raises(ParseError):
        rfs.read_xls_sheet(raw, "Sheet That Does Not Exist")


def _sst_record(strings: list[str], split_at: int) -> list[tuple[int, bytes]]:
    """Build an SST payload and cut it into SST + CONTINUE at `split_at` bytes, re-emitting the
    compression flag at the start of the CONTINUE exactly as BIFF8 requires when a string's
    characters straddle the boundary."""
    body = struct.pack("<II", len(strings), len(strings))
    for s in strings:
        body += struct.pack("<HB", len(s), 0) + s.encode("latin-1")
    return [(0x00FC, body[:split_at]), (0x003C, b"\x00" + body[split_at:])]


def test_shared_strings_survive_a_continue_boundary():
    """The SST is the one place a string can straddle a record boundary; a reader that simply
    concatenates the blocks loses a character there. The boundary here falls inside "MIDDLE"."""
    strings = ["FIRST FACILITY", "MIDDLE FACILITY NAME", "LAST"]
    records = _sst_record(strings, split_at=30)
    assert rfs._sst_strings(records, 0) == strings


def test_shared_strings_without_a_continue():
    strings = ["ONE", "TWO"]
    body = struct.pack("<II", 2, 2)
    for s in strings:
        body += struct.pack("<HB", len(s), 0) + s.encode("latin-1")
    assert rfs._sst_strings([(0x00FC, body)], 0) == strings


def test_rk_numbers_decode_both_encodings():
    assert rfs._rk_value(0x00000002 | (1234 << 2)) == pytest.approx(1234)
    assert rfs._rk_value(0x00000003 | (1234 << 2)) == pytest.approx(12.34)


# -------------------------------------------------------------------------------------- parsing
def test_parse_labels_the_repeated_facility_address_columns(frame):
    """The sheet repeats `Address 1`, `City`, `State` for the facility block; reading them by
    label would silently pick up the company's address (or, off by one, the postal code)."""
    assert "facility_city" in frame.columns and "facility_state" in frame.columns
    known = frame[frame["Facility Name"].astype(str).str.contains("ABSOLUTE ENERGY", na=False)]
    assert len(known) == 2  # the same facility is registered under two programs
    row = known[known["D Code"].astype(str).str.contains("D", na=False)].iloc[0]
    assert row["facility_state"] == "IA"
    assert row["facility_city"] == "ST ANSGAR"


def test_parse_rejects_a_workbook_whose_header_moved(raw, monkeypatch):
    monkeypatch.setattr(rfs, "COMPANY_SHEET", "RNG RIN Separator Information")
    with pytest.raises(ParseError):
        rfs.parse(raw)


def test_d_code_parsing():
    assert rfs.d_codes("D6, D3") == ["D3", "D6"]
    assert rfs.d_codes("D3") == ["D3"]
    assert rfs.d_codes("") == []
    assert rfs.d_codes(None) == []
    assert rfs.d_codes("no codes here") == []


# ------------------------------------------------------------------------------- feature rows
def test_only_registrations_with_a_d_code_become_rows(rows, frame):
    assert len(rows) < len(frame)
    assert all(codes for codes in rows["d_codes"])
    assert list(rows.columns) == rfs.COLUMNS


def test_a_facility_registered_twice_gets_one_row_with_the_union_of_its_codes(rows):
    absolute = rows[rows["facility_name"].str.contains("ABSOLUTE ENERGY", na=False)].iloc[0]
    assert absolute["d_codes"] == ["D3", "D6"]
    assert absolute["pathway_count"] == 2
    assert absolute["facility_state"] == "US-IA"


def test_keys_are_stable_and_unique(rows):
    assert rows["facility_key"].is_unique
    assert all(len(k) == 16 for k in rows["facility_key"])


def test_first_registered_year_is_null_with_a_flag_saying_why(rows):
    assert rows["first_registered_year"].isna().all()
    assert all(rfs.FIRST_REGISTERED_UNAVAILABLE in flags for flags in rows["feature_flags"])


def test_the_wave_d_codes_are_present_in_the_fixture(rows):
    seen = {code for codes in rows["d_codes"] for code in codes}
    assert set(rfs.WAVE_D_CODES) <= seen


def test_provenance_rides_every_row(rows):
    assert set(rows["source_id"]) == {"us.epa.rfs_public_data"}
    assert set(rows["licence"]) == {"public-domain"}
    assert set(rows["licence_id"]) == {"us.epa.rfs_public_data#test"}
    assert set(rows["source_url"]) == {rfs.REGISTRATION_URL}


def test_cli_prints_one_json_line_from_a_snapshot(tmp_path, capsys, monkeypatch):
    """Guards the summary line: pandas aggregates hand back numpy scalars, which `json.dumps`
    refuses without the repo's coercion."""
    monkeypatch.setattr(rfs.fuels, "CONTEXT_DIR", tmp_path)
    rfs.main(["--snapshot", str(FIXTURE), "--out", str(tmp_path / "out.parquet")])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["source_id"] == rfs.SOURCE_ID
    assert payload["facilities_with_d_codes"] > 0
