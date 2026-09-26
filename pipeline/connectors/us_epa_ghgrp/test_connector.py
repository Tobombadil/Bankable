"""Parser tests for us.epa.ghgrp against a recorded 36-row `pub_dim_facility` CSV fixture (real
RY2023 rows, 2026-09-25), plus the parent-string grammar, the personal-data exclusion, the
facility frame and a fetch driven by a fake session. No network (docs/20 §3.1)."""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot
from pipeline.connectors.us_epa_ghgrp.connector import (
    FACILITY_COLUMNS,
    PERSONAL_DATA_COLUMNS,
    PUB_DIM_FACILITY_COLUMNS,
    build_facilities,
    count_url,
    parse_parent_company,
    share_summary,
    strip_columns,
    subparts_list,
    window_url,
)

SOURCE_ID = "us.epa.ghgrp"
URL = "https://data.epa.gov/efservice/pub_dim_facility/year/2023/rows/0:9999/CSV"
FIXTURE = "epa_ghgrp_pub_dim_facility_2023.csv"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, URL, content_type="text/csv")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_fixture_is_the_real_column_set(parsed):
    _, raw, _, _ = parsed
    header = raw.content.decode("utf-8").splitlines()[0].split(",")
    assert tuple(header) == PUB_DIM_FACILITY_COLUMNS


def test_thirty_six_facility_rows(parsed):
    _, _, rows, df = parsed
    assert len(rows) == 36
    assert len(df) == 36


def test_personal_data_columns_never_reach_rows_raw_or_snapshot(parsed):
    c, raw, rows, df = parsed
    assert set(PERSONAL_DATA_COLUMNS) == {"address1", "address2", "comments"}
    for col in PERSONAL_DATA_COLUMNS:
        assert all(col not in r for r in rows)
        assert not df["raw"].str.contains(f'"{col}"').any()
    redacted = c.redact(raw.content)
    header = redacted.decode("utf-8").splitlines()[0].split(",")
    assert not set(PERSONAL_DATA_COLUMNS) & set(header)
    assert len(header) == len(PUB_DIM_FACILITY_COLUMNS) - len(PERSONAL_DATA_COLUMNS)
    # every other column, and every row, survives the redaction
    assert redacted.count(b"\n") == raw.content.count(b"\n")
    assert c.parse(RawSnapshot.from_file(__import__("conftest").fixture_path(FIXTURE), URL)) == rows


def test_record_id_is_facility_id_and_year(parsed):
    _, _, _, df = parsed
    assert not df["record_id"].duplicated().any()
    assert (df["source_id"] == SOURCE_ID).all()
    assert df["source_record_id"].str.fullmatch(r"\d+-2023").all()
    assert df["source_url"].str.contains("/pub_dim_facility/facility_id/").all()


def test_document_kind_fields(parsed):
    _, _, _, df = parsed
    assert (df["lifecycle_state"] == "filed").all()
    assert (df["doc_type"] == "ghgrp_facility_year").all()
    assert df["capacity_mw"].isna().all()
    ids = df["identifiers"].iloc[0]
    assert set(ids) == {"ghgrp_facility_id", "frs_id", "reporting_year", "naics_code"}


# ------------------------------------------------------------------ parent strings
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "TALLGRASS DEVELOPMENT LP (75%); PHILLIPS 66 (25%)",
            [("TALLGRASS DEVELOPMENT LP", 75.0), ("PHILLIPS 66", 25.0)],
        ),
        ("TALLGRASS DEVELOPMENT LP (100%)", [("TALLGRASS DEVELOPMENT LP", 100.0)]),
        ("US GOVERNMENT (%)", [("US GOVERNMENT", None)]),
        (
            "Garland Power & Light; City of Garland (100%);",
            [("Garland Power & Light", None), ("City of Garland", 100.0)],
        ),
        ("SOME COMPANY", [("SOME COMPANY", None)]),
        ("USA ENERGY GROUP LLC (98.88%)", [("USA ENERGY GROUP LLC", 98.88)]),
        ("", []),
        (None, []),
    ],
)
def test_parse_parent_company(text, expected):
    assert [(p["name"], p["share_pct"]) for p in parse_parent_company(text)] == expected


@pytest.mark.parametrize(
    ("text", "total", "flag"),
    [
        ("A (75%); B (25%)", 100.0, "ok"),
        ("A (75.7%)", 75.7, "not_100"),
        ("A (98.88%)", 98.88, "not_100"),
        ("A (%)", None, "unstated"),
        ("A; B (100%);", 100.0, "partial"),
        ("", None, "none"),
    ],
)
def test_share_summary_flags_but_never_normalises(text, total, flag):
    assert share_summary(parse_parent_company(text)) == (total, flag)


def test_subparts_drop_qualifiers():
    assert subparts_list("C,PP,RR (RPT),W") == ["C", "PP", "RR", "W"]
    assert subparts_list("RR (RPT),UU") == ["RR", "UU"]
    assert subparts_list(None) == []


# ------------------------------------------------------------------ facility frame
@pytest.fixture(scope="module")
def facilities(parsed):
    c, raw, rows, _ = parsed
    return build_facilities(rows, retrieved_at=raw.retrieved_at_iso, licence_id=c.source.licence_id)


def test_facility_frame_shape(facilities):
    assert list(facilities.columns) == FACILITY_COLUMNS
    assert len(facilities) == 36
    assert facilities["ghgrp_facility_id"].is_unique
    assert (facilities["reporting_year"] == 2023).all()
    assert facilities["lon"].notna().all() and facilities["lat"].notna().all()
    assert facilities["state_code"].str.fullmatch(r"US-[A-Z]{2}").all()
    assert (facilities["source_id"] == SOURCE_ID).all()


def test_tallgrass_rows_carry_the_stated_shares(facilities):
    tg = facilities[facilities["parent_company_raw"].str.contains("TALLGRASS", na=False)]
    assert len(tg) == 22
    joint = tg[tg["parent_count"] == 2]
    assert (joint["share_sum_pct"] == 100.0).all() and (joint["share_flag"] == "ok").all()
    assert {p["name"] for ps in joint["parents"] for p in ps} == {"TALLGRASS DEVELOPMENT LP", "PHILLIPS 66"}
    assert tg["rr_mrv_plan_url"].isna().all()
    assert tg["co2_captured"].isna().all()


def test_share_flags_on_the_real_rows(facilities):
    flags = facilities["share_flag"].value_counts().to_dict()
    assert flags["not_100"] >= 2  # Alliant 99.2, USA Energy Group 98.88
    assert flags["unstated"] >= 1  # US GOVERNMENT (%)
    assert flags["partial"] == 1  # Garland
    assert flags["none"] >= 1
    not_100 = facilities[facilities["share_flag"] == "not_100"]
    assert not (not_100["share_sum_pct"] == 100.0).any()


def test_mrv_and_subpart_membership(facilities):
    mrv = facilities[facilities["rr_mrv_plan_url"].notna()]
    assert {"Shute Creek Facility", "Archer Daniels Midland Co."} <= set(mrv["name"])
    assert mrv["subpart_rr"].all()
    hobbs = facilities[facilities["name"] == "Hobbs Field"].iloc[0]
    assert hobbs["subpart_rr"] and hobbs["subparts"] == ["RR"]  # EPA spells it `RR (RPT)`
    uu = facilities[facilities["reported_subparts"] == "RR (RPT),UU"]
    assert len(uu) == 1 and uu.iloc[0]["subpart_uu"] and uu.iloc[0]["subparts"] == ["RR", "UU"]
    assert facilities["co2_captured"].dropna().eq(True).all()


# ------------------------------------------------------------------ redaction helper
def test_strip_columns_keeps_quoting_and_rows():
    csv = b'a,b,c\n1,"x, y",3\n4,,6\n'
    assert strip_columns(csv, ("b",)) == b"a,c\n1,3\n4,6\n"
    assert strip_columns(csv, ("zzz",)) == csv


# ------------------------------------------------------------------ parse failures
def test_parse_rejects_html_and_layout_change():
    c = connector_for(SOURCE_ID)
    now = dt.datetime(2026, 9, 25, tzinfo=dt.UTC)
    html = RawSnapshot(b"<html>maintenance</html>", "text/html", URL, now, 200, "csv")
    with pytest.raises(ParseError):
        c.parse(html)
    wrong = RawSnapshot(b"foo,bar\n1,2\n", "text/csv", URL, now, 200, "csv")
    with pytest.raises(ParseError, match="layout changed"):
        c.parse(wrong)


# ------------------------------------------------------------------ fetch with a fake session
class _Resp:
    def __init__(self, status: int, body: bytes, ctype: str = "text/csv") -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.headers = {"Content-Type": ctype}

    def json(self):
        return json.loads(self.content)


class _Http:
    def __init__(self, counts: dict[int, int], windows: list[bytes]) -> None:
        self.counts = counts
        self.windows = windows
        self.urls: list[str] = []

    def get(self, url: str, **kwargs):
        self.urls.append(url)
        if "/count/JSON" in url:
            year = int(url.split("/year/")[1].split("/")[0])
            return _Resp(200, json.dumps([{"TOTALQUERYRESULTS": self.counts.get(year, 0)}]).encode(), "json")
        start, end = (int(v) for v in url.rsplit("/rows/", 1)[1].split("/")[0].split(":"))
        return _Resp(200, self.windows[start // (end - start + 1)])


def test_fetch_probes_the_newest_year_then_pages_the_windows(monkeypatch):
    fixture = __import__("conftest").fixture_path(FIXTURE).read_bytes()
    header, body = fixture.split(b"\n", 1)
    rows = body.strip().split(b"\n")
    # window 0 "full" is emulated by lowering WINDOW; the second window is the short one
    import pipeline.connectors.us_epa_ghgrp.connector as mod

    monkeypatch.setattr(mod, "WINDOW", 20)
    w0 = header + b"\n" + b"\n".join(rows[:20]) + b"\n"
    w1 = header + b"\n" + b"\n".join(rows[20:]) + b"\n"
    http = _Http({2023: len(rows)}, [w0, w1])
    c = connector_for(SOURCE_ID, http=http)
    raw = c.fetch()
    assert http.urls[:3] == [count_url(2026), count_url(2025), count_url(2024)]
    assert count_url(2023) in http.urls
    assert http.urls[-2:] == [window_url(2023, 0, 20), window_url(2023, 20, 20)]
    assert raw.meta["reporting_year"] == 2023
    assert raw.meta["expected_rows"] == len(rows)
    assert [w["rows"] for w in raw.meta["windows"]] == [20, len(rows) - 20]
    assert raw.content.count(b"\n") == len(rows) + 1  # one header
    parsed = c.parse(raw)
    assert len(parsed) == len(rows)
    assert raw.requests_made == len(http.urls)


def test_fetch_stops_on_an_empty_window_after_an_exactly_full_one(monkeypatch):
    fixture = __import__("conftest").fixture_path(FIXTURE).read_bytes()
    header, body = fixture.split(b"\n", 1)
    rows = body.strip().split(b"\n")
    import pipeline.connectors.us_epa_ghgrp.connector as mod

    monkeypatch.setattr(mod, "WINDOW", len(rows))  # window 0 is exactly full; window 1 is header-only
    http = _Http({2023: len(rows)}, [fixture, header + b"\n"])
    raw = connector_for(SOURCE_ID, http=http).fetch()
    assert [w["rows"] for w in raw.meta["windows"]] == [len(rows), 0]
    assert raw.content == fixture


def test_fetch_fails_closed_when_no_year_has_rows():
    c = connector_for(SOURCE_ID, http=_Http({}, []))
    with pytest.raises(ConnectorError, match="0 rows"):
        c.fetch()


def test_fetch_rejects_a_non_csv_window():
    class Html(_Http):
        def get(self, url: str, **kwargs):
            if "/count/JSON" in url:
                return super().get(url, **kwargs)
            return _Resp(200, b"<html>oops</html>", "text/html")

    c = connector_for(SOURCE_ID, http=Html({2023: 5}, []))
    with pytest.raises(ConnectorError, match="something other than"):
        c.fetch()


def test_pinned_reporting_year(monkeypatch):
    fixture = __import__("conftest").fixture_path(FIXTURE).read_bytes()
    http = _Http({2022: 36}, [fixture])
    c = connector_for(SOURCE_ID, http=http)
    monkeypatch.setattr(type(c), "reporting_year", 2022)
    raw = c.fetch()
    assert http.urls[0] == count_url(2022)
    assert raw.meta["reporting_year"] == 2022
    assert isinstance(pd.read_csv(__import__("io").BytesIO(raw.content)), pd.DataFrame)
