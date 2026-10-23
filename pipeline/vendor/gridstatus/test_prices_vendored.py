"""Pins the vendored price parsers' output on the recorded fixtures (README.md, "Pinning").

Each digest covers the frame's shape, column order, dtypes and every value, so a change to
`prices.py` that moves a price, a time stamp or a column fails here. The digests were taken from
`prices.py` as vendored (2026-10-22), whose logic is gridstatus 5c48466's statement for statement;
on 2026-10-23 upstream's own output on the same fixtures was found identical to these frames
(ERCOT real time once README change 5 is undone; README "Pinning").
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pandas as pd
import pytest

from pipeline.connectors.prices.conftest import fixture_bytes
from pipeline.vendor.gridstatus import prices as gs


def _digest(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(repr(list(df.columns)).encode())
    h.update(repr([str(t) for t in df.dtypes]).encode())
    h.update(pd.util.hash_pandas_object(df.reset_index(drop=True), index=True).values.tobytes())
    return h.hexdigest()[:16]


def ercot_rt_2025() -> pd.DataFrame:
    return gs.ercot_annual_spp(fixture_bytes("ercot_rtm_annual_2025_nov02.zip"), gs.REAL_TIME_15_MIN)


def ercot_da_2026() -> pd.DataFrame:
    return gs.ercot_annual_spp(fixture_bytes("ercot_dam_annual_2026_sample.zip"), gs.DAY_AHEAD_HOURLY)


def caiso_da() -> pd.DataFrame:
    frame = gs.caiso_oasis_frame(fixture_bytes("caiso_dam_2d.zip"))
    assert frame is not None
    return gs.caiso_lmp(frame, gs.DAY_AHEAD_HOURLY)


def caiso_rt() -> pd.DataFrame:
    frame = gs.caiso_oasis_frame(fixture_bytes("caiso_rtpd_1d.zip"))
    assert frame is not None
    return gs.caiso_lmp(frame, gs.REAL_TIME_15_MIN)


def nyiso_rt_month() -> pd.DataFrame:
    frames = []
    with zipfile.ZipFile(io.BytesIO(fixture_bytes("nyiso_20251101rtlbmp_zone_csv.zip"))) as z:
        for name in sorted(z.namelist()):
            frames.append(pd.read_csv(z.open(name)))
    out = [gs.nyiso_lmp(gs.nyiso_handle_time(f, "rtlbmp"), gs.REAL_TIME_HOURLY, "zone") for f in frames]
    return pd.concat(out, ignore_index=True)


def nyiso_da_day() -> pd.DataFrame:
    frame = pd.read_csv(io.BytesIO(fixture_bytes("nyiso_20261021damlbmp_zone.csv")))
    return gs.nyiso_lmp(gs.nyiso_handle_time(frame, "damlbmp"), gs.DAY_AHEAD_HOURLY, "zone")


def spp_da_portal() -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(fixture_bytes("spp_da_sl_20261021_trimmed.csv")))
    df.columns = df.columns.str.strip()
    return gs.spp_finalize_lmp(df, gs.DAY_AHEAD_HOURLY, include_baa=True)


PINNED = {
    "ercot_rt_2025": (ercot_rt_2025, (2300, 7), "eb2a71df94ef3d23"),
    "ercot_da_2026": (ercot_da_2026, (705, 7), "bbe803ba5ba30687"),
    "caiso_da": (caiso_da, (336, 11), "30d1397b6340ccde"),
    "caiso_rt": (caiso_rt, (672, 11), "b992afba1a8c186e"),
    "nyiso_rt_month": (nyiso_rt_month, (10815, 10), "a9d48876344ff689"),
    "nyiso_da_day": (nyiso_da_day, (360, 10), "7ff71e6ff06fe0f9"),
    "spp_da_portal": (spp_da_portal, (120, 12), "95d9a9446d7667fe"),
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_vendored_output_is_pinned(name: str) -> None:
    build, shape, digest = PINNED[name]
    df = build()
    assert (df.shape, _digest(df)) == (shape, digest)


def test_ercot_annual_column_spelling_change_is_absorbed() -> None:
    """README change 5: the 2025/2026 workbooks spell `Settlement Point Type` with spaces."""
    df = ercot_rt_2025()
    assert {"Location", "Location Type", "SPP", "Interval Start", "Interval End"} <= set(df.columns)
    assert set(df["Location Type"]) >= {"Trading Hub", "Load Zone"}


def test_layout_errors_are_value_errors() -> None:
    with pytest.raises(gs.PriceLayoutError):
        gs.ercot_annual_spp(b"PK\x05\x06" + b"\x00" * 18, gs.REAL_TIME_15_MIN)
    assert issubclass(gs.PriceLayoutError, ValueError)


@pytest.mark.parametrize(
    ("fixture", "market"),
    [
        ("ercot_rtm_annual_2025_nov02.zip", gs.REAL_TIME_15_MIN),
        ("ercot_rtm_annual_2026_sample.zip", gs.REAL_TIME_15_MIN),
        ("ercot_dam_annual_2025_nov02.zip", gs.DAY_AHEAD_HOURLY),
        ("ercot_dam_annual_2026_sample.zip", gs.DAY_AHEAD_HOURLY),
    ],
)
def test_the_sheet_by_sheet_parse_is_the_whole_workbook_parse(fixture: str, market: str) -> None:
    """README change 10: `ercot_annual_spp_sheets` gives `ercot_annual_spp`'s rows, values and
    dtypes, one sheet at a time (the DST days included: 2025-11-02 and 2026-03-08)."""
    content = fixture_bytes(fixture)
    whole = gs.ercot_annual_spp(content, market)
    pieces = [df for _name, df in gs.ercot_annual_spp_sheets(content, market)]
    joined = pd.concat(pieces, ignore_index=True).sort_values(["Interval Start", "Location"], kind="stable")
    expected = whole.sort_values(["Interval Start", "Location"], kind="stable")
    pd.testing.assert_frame_equal(
        joined.reset_index(drop=True).astype({"Location Type": str}),
        expected.reset_index(drop=True).astype({"Location Type": str}),
    )


def test_the_sheet_filter_reads_only_the_sheets_asked_for() -> None:
    content = fixture_bytes("ercot_rtm_annual_2026_sample.zip")
    asked: list[str] = []

    def keep(name: str) -> bool:
        asked.append(name)
        return name == "Oct"

    names = [name for name, _df in gs.ercot_annual_spp_sheets(content, gs.REAL_TIME_15_MIN, keep)]
    assert names == ["Oct"] and asked == ["Mar", "Oct"]
