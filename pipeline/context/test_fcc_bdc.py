"""Tests for `pipeline.context.fcc_bdc` against `fixtures/fcc_bdc_fixed_summary_sample.zip`.

The fixture is **synthetic**: no live FCC file could be fetched (the Public Data API needs an
account token; every fcc.gov page answers 403 to this egress, 2026-09-28). What is real is the
layout: the zip member name (`bdc_us_fixed_broadband_summary_by_geography_D25_15sep2026.csv`), the
14-column header and the vocabularies of `area_data_type`, `geography_type`, `biz_res` and
`technology` (all 15 groups) as they appear in third-party copies of the J24/J25/D25 files. Every
number is invented. 264 rows cover: seven counties with Total/Nontribal/Urban/Rural rows, one with
the full technology list (Travis, TX) and the others with Any Technology / Fiber / the
`Cable/Fiber` union group that must never be read as fiber; one id that lost its leading zero
(`1003`); a territory with no Census 1:20m polygon (`60010`, American Samoa); a county with no Fiber
row at all (`02164`); and National/State/CBSA/Congressional District/Tribal rows that must be
dropped while reading.

No network: `fetch` runs against a stub session.
"""

from __future__ import annotations

import io
import json
import pathlib
import zipfile

import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpFailed
from pipeline.connectors.store import Store
from pipeline.context import fcc_bdc

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "fcc_bdc_fixed_summary_sample.zip"
MEMBER = "bdc_us_fixed_broadband_summary_by_geography_D25_15sep2026.csv"
RETRIEVED = "2026-09-28T12:00:00Z"


@pytest.fixture(scope="module")
def frame():
    return fcc_bdc.parse(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def metrics(frame):
    return fcc_bdc.county_metrics(
        frame,
        retrieved_at=RETRIEVED,
        source_url=frame.attrs["filing"].data_page_url,
        licence_id="us.fcc.bdc.fixed_summary#test",
    )


def _csv_text() -> str:
    with zipfile.ZipFile(FIXTURE) as zf:
        return zf.read(MEMBER).decode("utf-8")


# ----------------------------------------------------------------------------------- vintage
def test_filing_is_read_from_the_file_name_never_the_fetch_date():
    f = fcc_bdc.filing_from_file_name(MEMBER)
    assert (f.as_of_date, f.vintage, f.vintage_label) == ("2025-12-31", "2025-12", "December 2025")
    assert f.revision == "2026-09-15"  # the FCC's processing date of the file, kept apart
    assert f.data_page_url.endswith("nationwide-data?version=dec2025")
    j = fcc_bdc.filing_from_file_name("bdc_us_fixed_broadband_summary_by_geography_J25_31mar2026.zip")
    assert (j.as_of_date, j.vintage_label, j.revision) == ("2025-06-30", "June 2025", "2026-03-31")


@pytest.mark.parametrize("name", ["summary.csv", "bdc_us_provider_summary_by_geography_D25_15sep2026.csv"])
def test_a_file_that_names_no_filing_is_refused(name):
    with pytest.raises(ParseError):
        fcc_bdc.filing_from_file_name(name)


# ------------------------------------------------------------------------------------- parse
def test_parse_keeps_only_county_rows_for_fiber_and_any_technology(frame):
    assert frame.attrs["rows_in"] == 264
    assert set(frame["geography_type"]) == {"County"}
    assert set(frame["technology"]) == {"Fiber", "Any Technology"}  # never "Cable/Fiber"
    assert set(frame["area_data_type"]) == {"Total", "Rural"}
    assert "National" in frame.attrs["geography_types_seen"]
    assert len(frame.attrs["technologies_seen"]) == 15


def test_parse_zero_pads_county_ids(frame):
    assert "01003" in set(frame["geography_id"])
    assert "1003" not in set(frame["geography_id"])


def test_parse_accepts_a_bare_csv_only_with_its_file_name():
    body = _csv_text().encode()
    with pytest.raises(ParseError, match="cannot tell the filing"):
        fcc_bdc.parse(body)
    assert len(fcc_bdc.parse(body, file_name=MEMBER)) == 50


def test_parse_fails_loudly_on_a_renamed_column():
    text = _csv_text().replace("speed_02_02", "speed_0_2", 1)
    with pytest.raises(ParseError, match="lacks columns"):
        fcc_bdc.parse(text.encode(), file_name=MEMBER)


def test_parse_refuses_shares_that_are_not_fractions():
    lines = _csv_text().splitlines()
    cells = lines[1].split(",")
    cells[-1] = "58.4"  # a percentage where a fraction belongs
    body = "\n".join([lines[0], ",".join(cells), *lines[2:]]).encode()
    with pytest.raises(ParseError, match=r"outside \[0, 1\]"):
        fcc_bdc.parse(body, file_name=MEMBER)


def test_parse_refuses_a_zip_with_two_csvs():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(MEMBER, _csv_text())
        zf.writestr("extra.csv", "a,b\n1,2\n")
    with pytest.raises(ParseError, match="expected one CSV"):
        fcc_bdc.parse(buf.getvalue())


# ----------------------------------------------------------------------------------- metrics
def test_one_row_per_county_with_the_fiber_row_speed_02_02_as_the_share(metrics):
    assert list(metrics.columns) == fcc_bdc.COLUMNS
    assert metrics["county_fips"].is_unique
    assert len(metrics) == 8
    autauga = metrics.set_index("county_fips").loc["01001"]
    assert autauga["county_name"] == "Autauga County"
    assert autauga["state_code"] == "US-AL"
    assert autauga["bsl_units"] == 28718
    assert autauga["fiber_share"] == pytest.approx(0.412346)  # Total / R / Fiber / speed_02_02
    assert autauga["fiber_share_business"] == pytest.approx(0.398765)  # Total / B / Fiber
    assert autauga["fiber_gigabit_share"] == pytest.approx(0.402)  # Total / R / Fiber / 1000_100
    assert autauga["served_100_20_share"] == pytest.approx(1.0)  # Any Technology / 100_20
    assert autauga["rural_bsl_units"] == 11905
    assert autauga["rural_fiber_share"] == pytest.approx(0.61)


def test_cable_fiber_union_is_not_counted_as_fiber(metrics):
    # In the fixture the Cable/Fiber group sits 0.25 above Fiber for every county.
    travis = metrics.set_index("county_fips").loc["48453"]
    assert travis["fiber_share"] == pytest.approx(0.701)


def test_a_county_without_a_fiber_row_is_unknown_not_zero(metrics):
    lake = metrics.set_index("county_fips").loc["02164"]
    assert pd.isna(lake["fiber_share"])
    assert lake["bsl_units"] == 1450
    assert metrics.attrs["counties_without_fiber_row"] == 1


def test_a_county_without_rural_rows_has_no_rural_metric(metrics):
    dc = metrics.set_index("county_fips").loc["11001"]
    assert pd.isna(dc["rural_bsl_units"])
    assert pd.isna(dc["rural_fiber_share"])


def test_every_row_carries_provenance_and_the_filing_not_the_fetch_date(metrics):
    for col in ("source_id", "source_url", "retrieved_at", "licence", "licence_id"):
        assert metrics[col].notna().all(), col
    assert set(metrics["source_id"]) == {"us.fcc.bdc.fixed_summary"}
    assert set(metrics["licence"]) == {"public-domain"}
    assert set(metrics["vintage"]) == {"2025-12"}
    assert not metrics["vintage"].str.startswith("2026-09").any()
    assert metrics["fcc_area_url"].iloc[0].endswith("type=county&geoid=01001")


def test_raw_traces_every_metric_to_the_source_cells(metrics):
    raw = json.loads(metrics.set_index("county_fips").loc["01001", "raw"])
    keys = ("technology", "biz_res", "area_data_type")
    fiber_r = [c for c in raw if tuple(c[k] for k in keys) == ("Fiber", "R", "Total")]
    assert len(fiber_r) == 1 and fiber_r[0]["speed_02_02"] == pytest.approx(0.412345678)


def test_duplicate_county_rows_are_an_error(frame):
    doubled = frame.iloc[[0, 0]].copy()
    doubled.attrs = dict(frame.attrs)
    with pytest.raises(ParseError, match="duplicate"):
        fcc_bdc.county_metrics(doubled, retrieved_at=RETRIEVED, source_url="u", licence_id="l")


# -------------------------------------------------------------------------------------- join
def test_join_coverage_lists_both_directions(metrics):
    regions = {"01001": "Autauga", "01003": "Baldwin", "09110": "Capitol"}
    cov = fcc_bdc.join_coverage(metrics, regions)
    assert cov["matched"] == 2
    assert "60010" in cov["fcc_without_polygon"]
    assert cov["polygons_without_fcc_row"] == 1
    assert cov["polygons_without_fcc_row_sample"] == ["09110"]


def test_join_against_the_vendored_county_polygons(metrics):
    cov = fcc_bdc.join_coverage(metrics, fcc_bdc.county_region_ids())
    assert cov["region_counties"] == 3222
    assert cov["fcc_without_polygon"] == ["60010"]  # American Samoa: outside the Census 1:20m file
    assert cov["matched"] == 7


# ------------------------------------------------------------------------------------- fetch
def test_credentials_come_only_from_the_environment():
    with pytest.raises(fcc_bdc.CredentialsMissing) as err:
        fcc_bdc.Credentials.from_env({})
    assert "FCC_BDC_USERNAME" in str(err.value) and "FCC_BDC_API_TOKEN" in str(err.value)
    with pytest.raises(fcc_bdc.CredentialsMissing, match="FCC_BDC_API_TOKEN"):
        fcc_bdc.Credentials.from_env({"FCC_BDC_USERNAME": "data-team@example.org"})
    creds = fcc_bdc.Credentials.from_env({"FCC_BDC_USERNAME": "u", "FCC_BDC_API_TOKEN": "t"})
    assert creds.headers() == {"username": "u", "hash_value": "t"}


def test_latest_availability_filing_ignores_other_data_types():
    dates = [
        {"data_type": "availability", "as_of_date": "2025-06-30"},
        {"data_type": "availability", "as_of_date": "2025-12-31"},
        {"data_type": "challenge", "as_of_date": "2026-06-30"},
    ]
    assert fcc_bdc.latest_availability_as_of(dates) == "2025-12-31"
    with pytest.raises(ParseError):
        fcc_bdc.latest_availability_as_of([{"data_type": "challenge", "as_of_date": "2026-01-01"}])


def test_pick_summary_file_takes_the_newest_revision_by_name():
    names = [
        "bdc_us_provider_summary_by_geography_D25_15sep2026",
        "bdc_us_fixed_broadband_summary_by_geography_D25_02jul2026",
        "bdc_us_fixed_broadband_summary_by_geography_D25_15sep2026",
        "bdc_01_fixed_broadband_summary_by_geography_place_D25_15sep2026",
    ]
    files = [{"file_id": str(i), "file_name": n, "file_type": "csv"} for i, n in enumerate(names, start=1)]
    assert fcc_bdc.pick_summary_file(files)["file_id"] == "3"
    with pytest.raises(ParseError):
        fcc_bdc.pick_summary_file(files[:1])


class _Resp:
    def __init__(self, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers: dict[str, str] = {}

    def json(self):
        return json.loads(self.content)


class _ApiSession:
    """The three Public Data API answers; records every call's URL, robots flag and headers."""

    def __init__(self, *, listed_under: str = "2025-12-31", status: int = 200) -> None:
        self.calls: list[tuple[str, bool, dict[str, str]]] = []
        self.requests_made = 0
        self.listed_under = listed_under
        self.status = status

    def get(self, url: str, *, honour_robots: bool = True, headers=None, **_: object) -> _Resp:
        self.calls.append((url, honour_robots, dict(headers or {})))
        self.requests_made += 1
        if self.status != 200:
            return _Resp(self.status, b'{"status":"fail","status_code":401,"message":"Unauthorized"}')
        if url.endswith("/listAsOfDates"):
            body = {"data": [{"data_type": "availability", "as_of_date": self.listed_under}]}
            return _Resp(200, json.dumps(body).encode())
        if "/listAvailabilityData/" in url:
            body = {
                "data": [
                    {"file_id": "900", "file_name": MEMBER.removesuffix(".csv"), "file_type": "csv",
                     "subcategory": "Summary by Geography Type - Other Geographies"},
                    {"file_id": "901", "file_name": "bdc_us_provider_summary_by_geography_D25_15sep2026",
                     "file_type": "csv", "subcategory": "Provider Summary by Geography Type"},
                ]
            }  # fmt: skip
            return _Resp(200, json.dumps(body).encode())
        assert url.endswith("/downloads/downloadFile/availability/900")
        return _Resp(200, FIXTURE.read_bytes())


CREDS = fcc_bdc.Credentials(username="role-account@example.org", token="t0k3n")  # noqa: S106 -- test value


def test_fetch_makes_three_api_calls_with_credentials_and_records_no_secret(tmp_path):
    session = _ApiSession()
    result = fcc_bdc.fetch(credentials=CREDS, session=session, store=Store(tmp_path))  # type: ignore[arg-type]
    assert [c[0].rsplit("/map/", 1)[1] for c in session.calls] == [
        "listAsOfDates",
        "downloads/listAvailabilityData/2025-12-31",
        "downloads/downloadFile/availability/900",
    ]
    assert all(c[1] is False for c in session.calls)  # API endpoints, not pages
    assert all(c[2] == CREDS.headers() for c in session.calls)
    assert result.filing.vintage == "2025-12"
    assert result.meta["subcategories"] == [
        "Provider Summary by Geography Type",
        "Summary by Geography Type - Other Geographies",
    ]
    assert result.snapshot_path is not None and result.snapshot_path.suffix == ".zip"
    run_records = list((tmp_path / "runs" / fcc_bdc.SOURCE_ID).glob("*.json"))
    assert len(run_records) == 1
    record_text = run_records[0].read_text()
    assert "t0k3n" not in record_text and "role-account" not in record_text


def test_fetch_refuses_a_file_from_another_filing():
    with pytest.raises(ParseError, match="listed under"):
        fcc_bdc.fetch(credentials=CREDS, session=_ApiSession(listed_under="2025-06-30"), write=False)  # type: ignore[arg-type]


def test_fetch_surfaces_the_401():
    with pytest.raises(HttpFailed, match="401"):
        fcc_bdc.fetch(credentials=CREDS, session=_ApiSession(status=401), write=False)  # type: ignore[arg-type]


def test_fetch_without_credentials_makes_no_request(monkeypatch):
    monkeypatch.delenv(fcc_bdc.ENV_USERNAME, raising=False)
    monkeypatch.delenv(fcc_bdc.ENV_TOKEN, raising=False)
    session = _ApiSession()
    with pytest.raises(fcc_bdc.CredentialsMissing):
        fcc_bdc.fetch(session=session, write=False)  # type: ignore[arg-type]
    assert session.calls == []


# --------------------------------------------------------------------------------------- run
def test_run_from_snapshot_writes_the_parquet_and_reports_coverage(tmp_path):
    out = tmp_path / "fiber.parquet"
    summary = fcc_bdc.run(snapshot=FIXTURE, out=out, store=Store(tmp_path))
    assert out.exists() and summary["parquet_bytes"] == out.stat().st_size
    assert summary["counties"] == 8 and summary["csv_rows"] == 264
    assert summary["vintage_label"] == "December 2025"
    assert summary["coverage"]["fcc_without_polygon"] == ["60010"]


def test_ranking_and_national_share_skip_unknown_counties(metrics):
    ranked = fcc_bdc.rank_counties(metrics, n=3)
    assert [r["county_fips"] for r in ranked["top"]] == ["38053", "48453", "11001"]
    assert [r["county_fips"] for r in ranked["bottom"]] == ["60010", "72127", "01001"]
    assert "02164" not in {r["county_fips"] for r in ranked["top"] + ranked["bottom"]}
    known = metrics.loc[metrics["fiber_share"].notna()]
    expected = (known["fiber_share"] * known["bsl_units"]).sum() / known["bsl_units"].sum()
    assert fcc_bdc.unit_weighted_fiber_share(metrics) == pytest.approx(expected, abs=1e-4)
