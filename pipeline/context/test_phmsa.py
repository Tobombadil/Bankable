"""Tests for `pipeline.context.phmsa` against trimmed copies of PHMSA's own files.

`fixtures/phmsa_annual_gt_sample.zip` carries the 2025 per-year workbook rebuilt from
`annual_gas_transmission_gathering_2010_present.zip` (archived capture of 2026-05-08,
109,916,599 bytes, sha256 5ed2ef12…) with twelve operators kept -- the Tallgrass group (Tallgrass
Interstate 1007, Trailblazer 19574, Rockies Express 32163, Ruby 40623, East Cheyenne Gas Storage
32655, Cheyenne Connector 39933) plus Tallgrass Midstream 39216 (a
gathering-only filer with no transmission miles), Northern Natural 13750, Tennessee Gas 19160,
Columbia Gulf 2620, Transwestern 19610 and Dow 3527 -- across Parts A-D, H, J and L. It also carries a
deliberately damaged `…_2026.xlsx`, mirroring the three members that are damaged in PHMSA's own
published zip, so the year fallback is exercised.

`fixtures/phmsa_incident_gt_sample.zip` is 285 real rows of
`incident_gas_transmission_gathering_jan2010_present.txt` (archived capture of 2026-05-08,
2,254,132 bytes) trimmed to twenty of its 624 columns -- the identity, year and outcome fields the
parser reads.

No test here touches the network: `fetch` is driven through a stub session.
"""

from __future__ import annotations

import json
import pathlib
import zipfile

import pytest

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpFailed
from pipeline.context import phmsa

FIXTURES = pathlib.Path(__file__).with_name("fixtures")
ANNUAL = FIXTURES / "phmsa_annual_gt_sample.zip"
INCIDENTS = FIXTURES / "phmsa_incident_gt_sample.zip"

TALLGRASS_INTERSTATE = "1007"
ROCKIES_EXPRESS = "32163"


@pytest.fixture(scope="module")
def annual():
    return phmsa.parse_annual(ANNUAL.read_bytes())


@pytest.fixture(scope="module")
def incidents():
    return phmsa.parse_incidents(INCIDENTS.read_bytes())


@pytest.fixture(scope="module")
def rows(annual, incidents):
    return phmsa.build_rows(annual, incidents, retrieved_at="2026-09-19T21:55:04Z")


# ------------------------------------------------------------------------------- annual parsing
def test_parse_annual_skips_a_damaged_newer_workbook_and_reports_it(annual):
    assert annual.report_year == 2025
    assert annual.member == "annual_gas_transmission_gathering_2025.xlsx"
    assert any("2026" in d for d in annual.damaged_members)


def test_parse_annual_reads_every_part_it_needs(annual):
    # 13 identity rows for 12 operators: one operator files a second report for another commodity,
    # which is why `build_rows` groups by OPERATOR_ID rather than trusting one row per operator.
    assert len(annual.identity) == 13
    assert annual.identity["OPERATOR_ID"].nunique() == 12
    for frame in (annual.class_miles, annual.decade, annual.diameter):
        assert "OPERATOR_ID" in frame.columns
        assert len(frame)
    assert annual.datafile_as_of == "2026-05-01"


def test_parse_annual_can_be_pinned_to_a_year():
    with pytest.raises(ParseError):
        phmsa.parse_annual(ANNUAL.read_bytes(), year=2019)


def test_a_zip_without_annual_workbooks_raises(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    with pytest.raises(ParseError):
        phmsa.parse_annual(path.read_bytes())


def test_operator_ids_normalise_across_the_two_files():
    # The workbook stores the id as a number, the incident file as text.
    assert phmsa._operator_id(1007.0) == "1007"
    assert phmsa._operator_id("1007") == "1007"
    assert phmsa._operator_id(None) is None


# ------------------------------------------------------------------------------ feature values
def test_one_row_per_operator_with_the_report_year(rows):
    assert len(rows) == 12
    assert rows["operator_id"].is_unique
    assert set(rows["report_year"]) == {2025}
    assert list(rows.columns) == phmsa.COLUMNS


def test_tallgrass_interstate_mileage_matches_the_filed_report(rows):
    row = rows.set_index("operator_id").loc[TALLGRASS_INTERSTATE]
    assert row["operator_name"] == "TALLGRASS INTERSTATE GAS TRANSMISSION, LLC"
    assert row["onshore_transmission_miles"] == pytest.approx(4308.3, abs=0.1)
    assert sum(row["miles_by_decade"].values()) == pytest.approx(4308.3, abs=0.5)
    assert sum(row["miles_by_diameter"].values()) == pytest.approx(4308.3, abs=0.5)


def test_decade_and_diameter_tables_use_the_documented_keys(rows):
    row = rows.set_index("operator_id").loc[ROCKIES_EXPRESS]
    assert set(row["miles_by_decade"]) <= set(phmsa.DECADE_COLUMNS.values())
    assert set(row["miles_by_diameter"]) <= set(phmsa.DIAMETER_COLUMNS.values())
    # Rockies Express was built in the 2000s; the vintage table should say so.
    assert row["miles_by_decade"]["2000s"] > 1000
    assert "pre_1940" not in row["miles_by_decade"]


def test_zero_valued_buckets_are_dropped_not_stored_as_zero(rows):
    for table in rows["miles_by_decade"]:
        assert all(v != 0.0 for v in table.values())


def test_an_operator_with_no_transmission_miles_is_kept(rows):
    # Tallgrass Midstream (39216) files a gathering-only report. Every filer stays in the frame, so
    # "no transmission miles" is a stated answer rather than a missing row.
    zero = rows.set_index("operator_id").loc["39216"]
    assert zero["onshore_transmission_miles"] == 0.0
    assert zero["miles_by_decade"] == {}
    assert rows["onshore_transmission_miles"].notna().all()


# ----------------------------------------------------------------------------- incident counts
def test_incident_window_ends_at_the_report_year(incidents):
    counts, years = phmsa.incident_counts(incidents, 2025)
    assert years == [2021, 2022, 2023, 2024, 2025]
    # 2026 rows exist in the fixture and must not be counted: the incident file is refreshed
    # monthly, so its newest year is partial.
    assert (incidents["IYEAR"] == 2026).any()
    counted = sum(c["incidents_5y_total"] for c in counts.values())
    in_window = incidents[incidents["IYEAR"].isin(years)]
    assert counted == in_window["REPORT_NUMBER"].nunique()


def test_incident_outcome_counts_are_the_stated_flags(incidents):
    counts, _ = phmsa.incident_counts(incidents, 2025)
    for entry in counts.values():
        assert entry["incidents_5y_with_fatality"] <= entry["incidents_5y_total"]
        assert entry["incidents_5y_with_injury"] <= entry["incidents_5y_total"]
    assert any(e["incidents_5y_with_ignition"] for e in counts.values())


def test_significant_count_is_null_with_a_flag_saying_why(rows):
    assert rows["incidents_5y_significant"].isna().all()
    assert all(phmsa.SIGNIFICANT_UNAVAILABLE in flags for flags in rows["feature_flags"])


def test_rows_without_the_incident_file_carry_null_counts(annual):
    rows = phmsa.build_rows(annual, None, retrieved_at="2026-09-19T21:55:04Z")
    assert rows["incidents_5y_total"].isna().all()
    assert any("incident file not read" in f for f in rows["feature_flags"].iloc[0])


def test_incident_parse_rejects_a_zip_without_the_table(tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("notes.pdf", "not the incident table")
    with pytest.raises(ParseError):
        phmsa.parse_incidents(path.read_bytes())


# -------------------------------------------------------------------------- fetch (stubbed HTTP)
class _Response:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {}

    def json(self):
        import json

        return json.loads(self.text)


class _StubSession:
    """Stands in for `PoliteSession`: PHMSA answers 403 (as it does from this environment) and the
    Archive serves the capture."""

    def __init__(self, *, cdx: str = "", availability: str = "", archived: bytes = b"PK\x03\x04data"):
        self.calls: list[str] = []
        self.cdx = cdx
        self.availability = availability
        self.archived = archived
        self.requests_made = 0

    def get(self, url: str, *, honour_robots: bool = True, **kwargs):
        self.calls.append(url)
        self.requests_made += 1
        if "cdx/search" in url:
            return _Response(200 if self.cdx else 404, self.cdx.encode())
        if "wayback/available" in url:
            return _Response(200, self.availability.encode(), {"Content-Type": "application/json"})
        if url.startswith("https://www.phmsa.dot.gov"):
            return _Response(
                403, b"<HTML><HEAD>\n<TITLE>Access Denied</TITLE>", {"Content-Type": "text/html"}
            )
        return _Response(
            200, self.archived, {"x-archive-orig-last-modified": "Thu, 07 May 2026 19:39:56 GMT"}
        )


def test_fetch_falls_back_to_the_archive_and_records_both_answers(tmp_path):
    from pipeline.connectors.store import Store

    session = _StubSession(cdx="20260101000000 100\n20260508070546 109916599\n")
    result = phmsa.fetch("annual", session=session, store=Store(tmp_path), write=True)
    assert result.via == "wayback"
    assert "20260508070546id_" in result.fetched_url
    # The published URL stays the recorded provenance; the archived URL is run-record detail.
    assert result.source_url == phmsa.ANNUAL_URL
    assert "403" in result.origin_result
    assert result.upstream_last_modified == "Thu, 07 May 2026 19:39:56 GMT"
    assert result.snapshot_path is not None and result.snapshot_path.exists()
    run = result.run_path
    assert run is not None and "annual" in run.name


def test_fetch_can_be_told_not_to_use_the_archive(tmp_path):
    from pipeline.connectors.store import Store

    session = _StubSession()
    with pytest.raises(HttpFailed):
        phmsa.fetch("annual", session=session, store=Store(tmp_path), write=False, allow_wayback=False)


def test_capture_lookup_prefers_cdx_and_falls_back_to_availability():
    session = _StubSession(cdx="20240101000000 1\n20260508070546 2\n")
    url, timestamp = phmsa.wayback_url(session, phmsa.ANNUAL_URL)
    assert timestamp == "20260508070546" and url.endswith(phmsa.ANNUAL_URL)

    # The availability API answers `{}` when handed a URL with its scheme, so the fallback strips it.
    availability = '{"archived_snapshots": {"closest": {"available": true, "timestamp": "20251112035633"}}}'
    session = _StubSession(cdx="", availability=availability)
    _, timestamp = phmsa.wayback_url(session, phmsa.ANNUAL_URL)
    assert timestamp == "20251112035633"
    assert any("wayback/available" in c and "https://www" not in c.split("url=")[-1] for c in session.calls)


def test_capture_lookup_raises_when_nothing_is_archived():
    session = _StubSession(cdx="", availability='{"archived_snapshots": {}}')
    with pytest.raises(HttpFailed):
        phmsa.wayback_url(session, phmsa.ANNUAL_URL)


def test_unknown_dataset_is_refused():
    with pytest.raises(KeyError):
        phmsa.fetch("hazardous_liquid", session=_StubSession(), write=False)


def test_cli_prints_one_json_line_from_snapshots(tmp_path, capsys):
    """Guards the summary line: `operators_with_incidents` is a pandas aggregate (numpy int64),
    which `json.dumps` refuses without the repo's coercion."""
    phmsa.main(
        [
            "--annual-snapshot", str(ANNUAL),
            "--incident-snapshot", str(INCIDENTS),
            "--out", str(tmp_path / "out.parquet"),
        ]
    )  # fmt: skip
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["source_id"] == phmsa.SOURCE_ID
    assert payload["report_year"] == 2025
    assert payload["operators"] == 12
    assert payload["operators_with_incidents"] >= 1


def test_snapshot_provenance_comes_from_the_run_record(tmp_path):
    """A re-parse of recorded bytes must keep the fetch time and the PHMSA URL. The snapshots of
    this source are named `<token>-<dataset>`, which the shared helper's token pattern does not
    match, so the run record is consulted first."""
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    session = _StubSession(cdx="20260508070546 109916599\n")
    result = phmsa.fetch("annual", session=session, store=store, write=True)
    assert result.snapshot_path is not None

    retrieved_at, source_url = phmsa.snapshot_provenance(
        result.snapshot_path, fallback_url=phmsa.ANNUAL_URL, store=store
    )
    assert retrieved_at == result.retrieved_at
    assert source_url == phmsa.ANNUAL_URL


def test_snapshot_provenance_falls_back_to_the_token_then_to_now(tmp_path):
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    token_named = tmp_path / "20260508T070546Z-annual.zip"
    token_named.write_bytes(b"PK\x03\x04")
    retrieved_at, _ = phmsa.snapshot_provenance(token_named, fallback_url=phmsa.ANNUAL_URL, store=store)
    assert retrieved_at == "2026-05-08T07:05:46Z"

    # A file that is not one of the store's snapshots: the bytes are real, the fetch time is not
    # knowable, so the read time is recorded rather than a fabricated one.
    loose = tmp_path / "hand-downloaded.zip"
    loose.write_bytes(b"PK\x03\x04")
    retrieved_at, url = phmsa.snapshot_provenance(loose, fallback_url=phmsa.ANNUAL_URL, store=store)
    assert retrieved_at.endswith("Z") and url == phmsa.ANNUAL_URL
