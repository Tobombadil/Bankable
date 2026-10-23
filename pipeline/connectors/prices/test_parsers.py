"""Parser tests for every price connector against recorded fixtures (docs/04 E-6; `fixtures/README.md`).

Each check names a fact of the source file it reads, so a parser change that moves a price, an
interval or a location fails here: interval counts on the days daylight saving starts and ends,
prices copied from the source rows, locations kept and dropped, provenance on every row.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.connectors.prices import PRICE_CONNECTORS, connector_class_for
from pipeline.connectors.prices.base import PRICE_COLUMNS
from pipeline.connectors.prices.conftest import snap
from pipeline.connectors.prices.eia_retail import RETAIL_COLUMNS, release_label
from pipeline.connectors.prices.ercot import parse_display_page
from pipeline.connectors.registry import Registry


def frame(source_id: str, fixture: str, **meta: object) -> pd.DataFrame:
    connector = connector_class_for(source_id)(Registry().get(source_id))
    raw = snap(fixture, **meta)
    rows = connector.parse(raw)
    return connector.normalize(rows, raw) if rows else pd.DataFrame(columns=PRICE_COLUMNS)


def utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def price_at(df: pd.DataFrame, location: str, start: str) -> float:
    rows = df[(df["location"] == location) & (df["interval_start"] == utc(start))]
    assert len(rows) == 1, rows
    return float(rows["price"].iloc[0])


# --------------------------------------------------------------------------------------------- ERCOT
ERCOT = "us.iso.ercot.prices"


def test_ercot_real_time_page_has_every_interval_of_a_normal_day() -> None:
    df = frame(ERCOT, "ercot_20261021_real_time_spp.html", kind="rt_page")
    assert len(df) == 96 * 15 and df["location"].nunique() == 15
    assert set(df["market"]) == {"rt"} and set(df["interval_minutes"]) == {15}
    # 10/21/2026 interval ending 0015 (CDT): HB_NORTH 21.39, LZ_WEST 52.42 (the page's first row).
    assert price_at(df, "HB_NORTH", "2026-10-21T05:00:00") == 21.39
    assert price_at(df, "LZ_WEST", "2026-10-21T05:00:00") == 52.42
    assert df["interval_start"].min() == utc("2026-10-21T05:00:00")
    assert df["interval_start"].max() == utc("2026-10-22T04:45:00")


def test_ercot_real_time_page_reads_the_repeated_hour_on_the_day_dst_ends() -> None:
    """2025-11-02: 100 intervals; `0100*` .. `0145*` are the standard-time pass."""
    df = frame(ERCOT, "ercot_20251102_real_time_spp.html", kind="rt_page")
    assert len(df) == 100 * 15
    north = df[df["location"] == "HB_NORTH"].sort_values("interval_start")
    assert north["interval_start"].is_unique
    assert north["interval_start"].diff().dropna().eq(pd.Timedelta(minutes=15)).all()


def test_ercot_real_time_page_on_the_day_dst_starts_has_92_intervals() -> None:
    df = frame(ERCOT, "ercot_20260308_real_time_spp.html", kind="rt_page")
    north = df[df["location"] == "HB_NORTH"].sort_values("interval_start")
    assert len(north) == 92
    assert north["interval_start"].diff().dropna().eq(pd.Timedelta(minutes=15)).all()


def test_ercot_day_ahead_page_on_the_day_dst_starts_has_23_hours() -> None:
    df = frame(ERCOT, "ercot_20260308_dam_spp.html", kind="da_page")
    assert len(df) == 23 * 15 and set(df["interval_minutes"]) == {60}
    north = df[df["location"] == "HB_NORTH"].sort_values("interval_start")
    assert north["interval_start"].diff().dropna().eq(pd.Timedelta(hours=1)).all()


def test_ercot_day_ahead_page_for_tomorrow() -> None:
    df = frame(ERCOT, "ercot_20261022_dam_spp.html", kind="da_page")
    # 10/22/2026 hour ending 1: HB_BUSAVG 30.09 (the page's first value).
    assert price_at(df, "HB_BUSAVG", "2026-10-22T05:00:00") == 30.09
    assert len(df) == 24 * 15


def test_an_empty_ercot_dated_page_is_no_rows_not_an_error() -> None:
    """ERCOT keeps the 2025-11-02 day-ahead page but no longer fills it."""
    assert frame(ERCOT, "ercot_20251102_dam_spp.html", kind="da_page").empty


def test_the_display_page_parser_reads_the_header_and_the_update_time() -> None:
    from pipeline.connectors.prices.conftest import fixture_bytes

    header, rows, updated = parse_display_page(fixture_bytes("ercot_real_time_spp.html"))
    assert header[:2] == ["Oper Day", "Interval Ending"] and len(header) == 17
    assert rows and updated and updated.startswith("Oct 22, 2026")


def test_ercot_annual_workbook_keeps_the_fifteen_locations_and_drops_energy_weighted_zones() -> None:
    df = frame(ERCOT, "ercot_rtm_annual_2025_nov02.zip", kind="annual", market="rt", report="RTMLZHBSPP_2025")
    assert len(df) == 100 * 15
    assert not df["location"].str.endswith("_EW").any()
    assert df["location"].nunique() == 15


def test_the_annual_workbook_and_the_display_page_agree_on_the_same_day() -> None:
    """Two routes to the same prices (NP6-785-ER and the cdr display page) give the same values
    for every location and interval of 2025-11-02: the LZ (not LZEW) zone price, DST included."""
    workbook = frame(ERCOT, "ercot_rtm_annual_2025_nov02.zip", kind="annual", market="rt")
    page = frame(ERCOT, "ercot_20251102_real_time_spp.html", kind="rt_page")
    joined = workbook.merge(page, on=["location", "interval_start"], suffixes=("_wb", "_page"))
    assert len(joined) == 1500
    assert (joined["price_wb"] - joined["price_page"]).abs().max() < 1e-9


def test_ercot_annual_day_ahead_workbook() -> None:
    df = frame(ERCOT, "ercot_dam_annual_2026_sample.zip", kind="annual", market="da")
    assert len(df) == (23 + 24) * 15 and set(df["market"]) == {"da"}
    df25 = frame(ERCOT, "ercot_dam_annual_2025_nov02.zip", kind="annual", market="da")
    assert len(df25) == 25 * 15  # the day DST ends has 25 hours


def test_ercot_document_list_gives_the_annual_workbooks() -> None:
    import datetime as dt

    from pipeline.connectors.prices.base import Coverage, FetchRequest
    from pipeline.connectors.prices.ercot import Connector

    c = Connector(Registry().get(ERCOT))
    raw = snap("ercot_doclist_13061.json")
    request = FetchRequest(key="list-13061", url="u", ext="json", meta={"kind": "doc_list", "market": "rt"})
    follow = c.follow_up(request, raw, dt.date(2026, 10, 22), Coverage())
    assert [r.meta["report"] for r in follow] == ["RTMLZHBSPP_2025", "RTMLZHBSPP_2026"]
    assert [r.meta["doc_id"] for r in follow] == ["1177737535", "1287900508"]
    assert all(r.reuse_key and r.url.endswith(f"doclookupId={r.meta['doc_id']}") for r in follow)


# ----------------------------------------------------------------------------------------------- SPP
SPP = "us.iso.spp.prices"


def test_spp_day_ahead_map_features() -> None:
    df = frame(SPP, "spp_DA_Features_hubs.json", kind="features", market="da", minutes=60)
    assert len(df) == 72 and df["location"].nunique() == 3
    # GMTINTERVALEND 1792818000000 (2026-10-24T05:00Z) is hour ending 00:00 CDT: SPPSOUTH_HUB -24.7726.
    assert price_at(df, "SPPSOUTH_HUB", "2026-10-24T04:00:00") == pytest.approx(-24.7726)


def test_spp_real_time_map_features_are_five_minute() -> None:
    df = frame(SPP, "spp_RTBM_Features_hubs.json", kind="features", market="rt", minutes=5)
    assert len(df) == 3 and set(df["interval_minutes"]) == {5}
    assert df["interval_start"].nunique() == 1


def test_spp_portal_day_ahead_file_keeps_only_hubs() -> None:
    df = frame(SPP, "spp_da_sl_20261021_trimmed.csv", kind="portal_da", market="da")
    assert len(df) == 72 and set(df["location"]) == {"SPPNORTH_HUB", "SPPSOUTH_HUB", "SWPW_HUB"}
    assert df["interval_start"].min() == utc("2026-10-21T05:00:00")


def test_spp_feature_without_an_interval_end_is_a_parse_error() -> None:
    import datetime as dt

    from pipeline.connectors.base import RawSnapshot
    from pipeline.connectors.prices.spp import Connector

    c = Connector(Registry().get(SPP))
    bad = RawSnapshot(
        content=json.dumps({"features": [{"attributes": {"SETTLEMENT_LOCATION": "SPPNORTH_HUB"}}]}).encode(),
        content_type="application/json",
        url="u",
        retrieved_at=dt.datetime(2026, 10, 22, tzinfo=dt.UTC),
        http_status=200,
        ext="json",
        meta={"kind": "features", "market": "da", "minutes": 60},
    )
    with pytest.raises(ParseError):
        c.parse(bad)


# --------------------------------------------------------------------------------------------- CAISO
CAISO = "us.iso.caiso.oasis_prices"


def test_caiso_day_ahead_two_days_seven_nodes() -> None:
    df = frame(CAISO, "caiso_dam_2d.zip", kind="oasis", market="da")
    assert len(df) == 7 * 48 and df["location"].nunique() == 7
    # INTERVALSTARTTIME_GMT 2026-10-19T08:00Z, DLAP_PGAE-APND LMP 49.46396 (the file's first row).
    assert price_at(df, "DLAP_PGAE-APND", "2026-10-19T08:00:00") == pytest.approx(49.46396)


def test_caiso_fifteen_minute_real_time() -> None:
    df = frame(CAISO, "caiso_rtpd_1d.zip", kind="oasis", market="rt")
    assert len(df) == 7 * 96 and set(df["interval_minutes"]) == {15}
    assert price_at(df, "DLAP_PGAE-APND", "2026-10-20T07:00:00") == pytest.approx(64.79206)


def test_an_oasis_error_document_is_a_parse_error_unless_optional() -> None:
    import datetime as dt

    from pipeline.connectors.base import RawSnapshot
    from pipeline.connectors.prices.caiso import Connector

    c = Connector(Registry().get(CAISO))

    def raw(optional: bool) -> RawSnapshot:
        return RawSnapshot(
            content=b"PK\x03\x04 INVALID_REQUEST.xml",
            content_type="application/zip",
            url="u",
            retrieved_at=dt.datetime(2026, 10, 22, tzinfo=dt.UTC),
            http_status=200,
            ext="zip",
            meta={"kind": "oasis", "market": "da", "optional": optional},
        )

    with pytest.raises(ParseError):
        c.parse(raw(False))
    assert c.parse(raw(True)) == []


# --------------------------------------------------------------------------------------------- NYISO
NYISO = "us.iso.nyiso.prices"


def test_nyiso_daily_zone_file_drops_the_external_proxies() -> None:
    df = frame(NYISO, "nyiso_20261021damlbmp_zone.csv", kind="day_csv", market="da")
    assert df["location"].nunique() == 11 and len(df) == 11 * 24
    assert not {"H Q", "NPX", "O H", "PJM"} & set(df["location"])
    # 10/21/2026 00:00 (EDT) CAPITL 37.65.
    assert price_at(df, "CAPITL", "2026-10-21T04:00:00") == 37.65


def test_nyiso_monthly_zip_spans_the_month_dst_included() -> None:
    """November 2025: 30 days and one repeated hour, 721 hours per zone."""
    df = frame(NYISO, "nyiso_20251101rtlbmp_zone_csv.zip", kind="month_zip", market="rt")
    assert len(df) == 11 * 721
    capital = df[df["location"] == "CAPITL"]
    assert capital["interval_start"].is_unique


def test_nyiso_keeps_the_published_congestion_sign_in_raw() -> None:
    df = frame(NYISO, "nyiso_20261021damlbmp_zone.csv", kind="day_csv", market="da")
    row = json.loads(df[df["location"] == "CENTRL"].sort_values("interval_start")["raw"].iloc[0])
    assert row["Marginal Cost Congestion ($/MWHr)"] == -0.15  # as NYISO prints it


# -------------------------------------------------------------------------------------------- Elexon
ELEXON = "gb.elexon.bmrs_prices"


def test_elexon_settlement_periods_on_the_day_uk_clocks_go_forward() -> None:
    df = frame(ELEXON, "elexon_system_prices_20260329.json", kind="system_prices", market="rt")
    assert len(df) == 46 and set(df["currency"]) == {"GBP"} and set(df["interval_minutes"]) == {30}


def test_elexon_price_is_the_system_sell_price() -> None:
    df = frame(ELEXON, "elexon_system_prices_20261021.json", kind="system_prices", market="rt")
    assert len(df) == 48
    # settlementPeriod 1, startTime 2026-10-20T23:00:00Z, systemSellPrice 41.5.
    assert price_at(df, "SYSTEM", "2026-10-20T23:00:00") == 41.5


# ----------------------------------------------------------------------------------------------- EIA
EIA = "us.eia.retail_sales"


def test_eia_retail_rows_by_state_sector_and_month() -> None:
    connector = connector_class_for(EIA)(Registry().get(EIA))
    raw = snap("eia_sales_revenue_trimmed.xlsx", kind="workbook")
    df = connector.normalize(connector.parse(raw), raw)
    assert list(df.columns) == RETAIL_COLUMNS
    assert set(df["state"]) == {"TX", "CA", "NY", "OK", "NE", "DC"}
    assert len(df) == 6 * 13 * 5  # 2025-07 .. 2026-07, five sectors
    ca = df[(df["state"] == "CA") & (df["sector"] == "residential") & (df["period"] == "2026-07")].iloc[0]
    assert ca["price_cents_kwh"] == 33.61 and ca["data_status"] == "Preliminary"
    assert not df["record_id"].duplicated().any()


def test_eia_release_label_reads_the_last_modified_header() -> None:
    assert release_label("Thu, 24 Sep 2026 13:48:22 GMT") == "2026-09-24"
    assert release_label("garbage") is None and release_label(None) is None


# ------------------------------------------------------------------------------------- every source
CASES = [
    (ERCOT, "ercot_20261021_real_time_spp.html", {"kind": "rt_page"}),
    (SPP, "spp_DA_Features_hubs.json", {"kind": "features", "market": "da", "minutes": 60}),
    (CAISO, "caiso_dam_2d.zip", {"kind": "oasis", "market": "da"}),
    (NYISO, "nyiso_20261021rtlbmp_zone.csv", {"kind": "day_csv", "market": "rt"}),
    (ELEXON, "elexon_system_prices_20261021.json", {"kind": "system_prices", "market": "rt"}),
]


@pytest.mark.parametrize(("source_id", "fixture", "meta"), CASES)
def test_every_row_carries_the_provenance_quartet_and_a_stable_record_id(source_id, fixture, meta) -> None:
    df = frame(source_id, fixture, **meta)
    assert list(df.columns) == PRICE_COLUMNS
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert set(df["source_id"]) == {source_id}
    assert not df["record_id"].duplicated().any()
    assert df["record_id"].str.startswith(f"{source_id}:").all()
    assert df["interval_start"].dt.tz is not None


def test_the_family_serves_exactly_the_six_built_rows() -> None:
    registry = Registry()
    for source_id in PRICE_CONNECTORS:
        entry = registry.get(source_id)
        assert entry.raw.get("runner") == "prices", source_id
        assert entry.raw.get("status") not in ("pending", "excluded"), source_id
    assert len(PRICE_CONNECTORS) == 6


@pytest.mark.parametrize(
    ("fixture", "market"),
    [
        ("ercot_rtm_annual_2025_nov02.zip", "rt"),
        ("ercot_rtm_annual_2026_sample.zip", "rt"),
        ("ercot_dam_annual_2025_nov02.zip", "da"),
        ("ercot_dam_annual_2026_sample.zip", "da"),
    ],
)
def test_the_column_wise_annual_frame_is_the_row_wise_one(fixture: str, market: str) -> None:
    """`Connector.frames` builds an annual sheet's rows column by column (review 2026-10-23,
    memory); the result, `raw` included, is what `normalize(parse(raw))` builds row by row."""
    connector = connector_class_for(ERCOT)(Registry().get(ERCOT))
    raw = snap(fixture, kind="annual", market=market, report=f"X_{market}")
    row_wise = connector.normalize(connector.parse(raw), raw)
    column_wise = pd.concat(list(connector.frames(raw)), ignore_index=True)
    order = ["location", "market", "interval_start"]
    pd.testing.assert_frame_equal(
        column_wise.sort_values(order).reset_index(drop=True),
        row_wise.sort_values(order).reset_index(drop=True),
        check_dtype=False,
    )
    assert (column_wise["raw"].map(json.loads).map(len) == 7).all()
