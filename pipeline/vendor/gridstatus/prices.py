# Copyright 2022 James Max Kanter. BSD 3-Clause licence: see LICENSE in this directory.
# Vendored from gridstatus main at commit 5c48466720a865f0f90c3cc9f7ed67f84d059b71 (2026-10-22;
# https://github.com/gridstatus/gridstatus); what was taken, from where, and every change is listed
# in README.md beside this file.
"""Price parsers for ERCOT, CAISO, NYISO and SPP, vendored from gridstatus main (5c48466).

Parse-only: each function takes bytes or a frame a connector already fetched (through
`pipeline.connectors.http.PoliteSession`, never `requests` directly) and returns the frame the
upstream method built from the same input. Nothing here touches the network (docs/20 §3.1). The
logic is upstream's, statement for statement; the changes are listed in README.md: the input
(bytes or a frame instead of a URL), typed module functions instead of methods, a `PriceLayoutError`
(a `ValueError`) where upstream used `assert` or raised a bare `ValueError`, and the one upstream
network call inside a parser (ERCOT's settlement-point mapping, which only decides the "Resource
Node" label) replaced by an argument.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pytz  # type: ignore[import-untyped]  # pandas 2's own dependency; no stubs installed
from pandas.errors import ParserError
from pytz.exceptions import NonExistentTimeError  # type: ignore[import-untyped]

__all__ = [
    "CAISO_TIMEZONE",
    "CAISO_TRADING_HUB_LOCATIONS",
    "DAY_AHEAD_HOURLY",
    "ERCOT_TIMEZONE",
    "NYISO_TIMEZONE",
    "REAL_TIME_5_MIN",
    "REAL_TIME_15_MIN",
    "REAL_TIME_HOURLY",
    "SPP_LMP_HUBS_AND_INTERFACES",
    "SPP_TIMEZONE",
    "ErcotDocument",
    "PriceLayoutError",
    "caiso_lmp",
    "caiso_oasis_frame",
    "ercot_annual_spp",
    "ercot_annual_spp_sheets",
    "ercot_documents",
    "ercot_finalize_spp",
    "ercot_parse_doc",
    "ercot_timestamp_from_friendly_name",
    "nyiso_handle_time",
    "nyiso_lmp",
    "spp_finalize_lmp",
    "spp_market_end_to_interval",
]

# gridstatus/base.py:29-46 `Markets` values used here.
REAL_TIME_5_MIN = "REAL_TIME_5_MIN"
REAL_TIME_15_MIN = "REAL_TIME_15_MIN"
REAL_TIME_HOURLY = "REAL_TIME_HOURLY"
DAY_AHEAD_HOURLY = "DAY_AHEAD_HOURLY"

# Each ISO class's `default_timezone`.
ERCOT_TIMEZONE = "US/Central"  # gridstatus/ercot.py:523
CAISO_TIMEZONE = "US/Pacific"  # gridstatus/caiso/caiso.py:174
NYISO_TIMEZONE = "US/Eastern"  # gridstatus/nyiso.py:114
SPP_TIMEZONE = "US/Central"  # gridstatus/spp.py:192

# gridstatus/ercot.py:84-89.
ERCOT_LOCATION_TYPE_HUB = "Trading Hub"
ERCOT_LOCATION_TYPE_RESOURCE_NODE = "Resource Node"
ERCOT_LOCATION_TYPE_ZONE = "Load Zone"
ERCOT_LOCATION_TYPE_ZONE_EW = "Load Zone Energy Weighted"
ERCOT_LOCATION_TYPE_ZONE_DC = "Load Zone DC Tie"
ERCOT_LOCATION_TYPE_ZONE_DC_EW = "Load Zone DC Tie Energy Weighted"

# gridstatus/caiso/caiso.py:186-190 `CAISO.trading_hub_locations`.
CAISO_TRADING_HUB_LOCATIONS: tuple[str, ...] = ("TH_NP15_GEN-APND", "TH_SP15_GEN-APND", "TH_ZP26_GEN-APND")

# gridstatus/nyiso.py:30.
NYISO_REFERENCE_BUS_LOCATION = "NYISO_LBMP_REFERENCE"
# gridstatus/nyiso.py:39-41 and 87-104 `DATASET_INTERVAL_MAP` rows for the two price datasets used.
NYISO_DAM_LMP_DATASET = "damlbmp"
NYISO_REAL_TIME_HOURLY_LMP_DATASET = "rtlbmp"
NYISO_DATASET_INTERVAL_MAP: dict[str, tuple[str, int | None]] = {
    NYISO_DAM_LMP_DATASET: ("start", 60),
    NYISO_REAL_TIME_HOURLY_LMP_DATASET: ("start", 60),
}

# gridstatus/spp.py:97-123 `LMP_HUBS_AND_INTERFACES`, unchanged (SWPW_HUB is not in it upstream;
# `spp_finalize_lmp` labels it "Settlement Location", as upstream does).
SPP_LMP_HUBS_AND_INTERFACES: dict[str, str] = {
    "AECI": "Interface",
    "ALTW": "Interface",
    "AMRN": "Interface",
    "BLKW": "Interface",
    "CLEC": "Interface",
    "DPC": "Interface",
    "EDDY": "Interface",
    "EES": "Interface",
    "ERCOTE": "Interface",
    "ERCOTN": "Interface",
    "GRE": "Interface",
    "LAM345": "Interface",
    "MCWEST": "Interface",
    "MDU": "Interface",
    "MEC": "Interface",
    "MISO": "Interface",
    "NSP": "Interface",
    "OTP": "Interface",
    "RCEAST": "Interface",
    "SCSE": "Interface",
    "SGE": "Interface",
    "SPA": "Interface",
    "SPC": "Interface",
    "SPPNORTH_HUB": "Hub",
    "SPPSOUTH_HUB": "Hub",
}
SPP_LOCATION_TYPE_SETTLEMENT_LOCATION = "Settlement Location"  # gridstatus/spp.py:63


class PriceLayoutError(ValueError):
    """The file lacks a column or shape the parser relies on (upstream: AssertionError/ValueError)."""


# ============================================================================================ ERCOT
@dataclass(frozen=True)
class ErcotDocument:
    """gridstatus/ercot.py:483-490 `Document`."""

    url: str
    publish_date: pd.Timestamp
    constructed_name: str
    friendly_name: str
    friendly_name_timestamp: pd.Timestamp | None
    doc_id: str
    content_size: int | None


def ercot_timestamp_from_friendly_name(friendly_name: str) -> pd.Timestamp:
    """gridstatus/ercot.py:493-516 `parse_timestamp_from_friendly_name`."""
    parts = friendly_name.replace("_retry", "").split("_")
    date_str = parts[1]
    time_str = parts[2]
    # Add a colon between hours, minutes, and seconds for pandas to parse
    if len(time_str) > 4:
        second_str = time_str[4:6]
    else:
        second_str = "00"

    time_str_formatted = time_str[:2] + ":" + time_str[2:4] + ":" + second_str

    # Combine date and time strings
    datetime_str = date_str + " " + time_str_formatted
    return pd.to_datetime(datetime_str, format="%Y%m%d %H:%M:%S").tz_localize(
        ERCOT_TIMEZONE,
        ambiguous=False,
    )


def ercot_documents(
    doc_list: Mapping[str, Any],
    *,
    constructed_name_contains: str | None = None,
    extension: str | None = None,
    base_url: str = "www.ercot.com",
) -> list[ErcotDocument]:
    """The parse half of gridstatus/ercot.py:7040-7164 `Ercot._get_documents`: the MIS document
    list (`IceDocListJsonWS`) as `ErcotDocument`s, filtered by constructed name and extension.
    Upstream's date filters are the caller's (the connector decides which documents it needs), and
    an empty match returns `[]` rather than raising `NoDataFoundException`."""
    try:
        docs = doc_list["ListDocsByRptTypeRes"]["DocumentList"]
    except (KeyError, TypeError) as exc:
        raise PriceLayoutError(f"not an ERCOT document list: {exc!r}") from exc
    matches: list[ErcotDocument] = []
    for doc in docs:
        document = doc["Document"]
        doc_url = f"https://{base_url}/misdownload/servlets/mirDownload?doclookupId={document['DocID']}"
        # make sure to handle retry files
        # e.g SPPHLZNP6905_retry_20230608_1545_csv
        try:
            friendly_name_timestamp: pd.Timestamp | None = ercot_timestamp_from_friendly_name(
                document["FriendlyName"],
            )
        except Exception:
            friendly_name_timestamp = None

        friendly_name = document["FriendlyName"]

        # ERCOT adds xhr to the second set of file names during the repeated hour
        # for DST end. However, ERCOT may get the timezone offset wrong in the
        # PublishDate. Therefore, we remove the ERCOT provided timezone offset then
        # re-add the offset accounting for the repeated hour.
        publish_date = (
            pd.Timestamp(document["PublishDate"])
            .tz_localize(None)
            .tz_localize(
                ERCOT_TIMEZONE,
                # Pandas wants ambiguous to be True when DST is True (Pandas only
                # uses ambiguous during the repeated hour) The "xhr" file occurs
                # after the clock has been set back an hour so is not in DST.
                ambiguous="xhr" not in friendly_name,
            )
        )
        size = document.get("ContentSize")
        doc_obj = ErcotDocument(
            url=doc_url,
            publish_date=publish_date,
            constructed_name=document["ConstructedName"],
            friendly_name=friendly_name,
            friendly_name_timestamp=friendly_name_timestamp,
            doc_id=str(document["DocID"]),
            content_size=int(size) if str(size or "").isdigit() else None,
        )
        match = True
        if extension:
            match = match and doc_obj.friendly_name.endswith(extension)
        if constructed_name_contains:
            match = match and constructed_name_contains in doc_obj.constructed_name
        if match:
            matches.append(doc_obj)
    return matches


def _ercot_ambiguous_based_on_dstflag(df: pd.DataFrame) -> pd.Series:
    """gridstatus/ercot.py:7466-7479 `ambiguous_based_on_dstflag`."""
    # DSTFlag is Y during the repeated hour (after the clock has been set back)
    # so it's False/N during DST And True/Y during Standard Time.
    # For ambiguous, Pandas wants True for DST and False for Standard Time
    # during repeated hours. Therefore, ambgiuous should be True when
    # DSTFlag is False/N

    # Some ERCOT datasets use a boolean, some use a string
    if df["DSTFlag"].dtype == bool:
        return ~df["DSTFlag"]
    # Assume that if the DSTFlag column is a string, it's "Y" or "N"
    if not set(df["DSTFlag"].unique()).issubset({"Y", "N"}):
        raise PriceLayoutError(f"DSTFlag values outside Y/N: {sorted(map(str, df['DSTFlag'].unique()))}")
    return df["DSTFlag"] == "N"


def ercot_parse_doc(
    doc: pd.DataFrame,
    dst_ambiguous_default: str = "infer",
    nonexistent: str = "raise",
) -> pd.DataFrame:
    """gridstatus/ercot.py:7481-7632 `Ercot.parse_doc`: delivery date and hour (and 15-minute
    interval) columns to `Time`, `Interval Start`, `Interval End` in US/Central."""
    # files sometimes have different naming conventions
    # a more elegant solution would be nice

    doc.rename(
        columns={
            "deliveryDate": "DeliveryDate",
            "Delivery Date": "DeliveryDate",
            "DELIVERY_DATE": "DeliveryDate",
            "OperDay": "DeliveryDate",
            "hourEnding": "HourEnding",
            "Hour Ending": "HourEnding",
            "HOUR_ENDING": "HourEnding",
            "Repeated Hour Flag": "DSTFlag",
            "RepeatedHourFlag": "DSTFlag",
            "Date": "DeliveryDate",
            "DeliveryHour": "HourEnding",
            "Delivery Hour": "HourEnding",
            "Delivery Interval": "DeliveryInterval",
            # fix whitespace in column name
            "DSTFlag    ": "DSTFlag",
        },
        inplace=True,
    )

    original_cols = doc.columns.tolist()

    ending_time_col_name = "HourEnding"

    ambiguous: Any = dst_ambiguous_default
    if "DSTFlag" in doc.columns:
        ambiguous = _ercot_ambiguous_based_on_dstflag(doc)

    # i think DeliveryInterval only shows up
    # in 15 minute data along with DeliveryHour
    if "DeliveryInterval" in original_cols:
        interval_length = pd.Timedelta(minutes=15)

        doc["HourBeginning"] = doc[ending_time_col_name] - 1

        doc["Interval Start"] = (
            pd.to_datetime(doc["DeliveryDate"])
            + doc["HourBeginning"].astype(int) * pd.Timedelta(hours=1)
            + ((doc["DeliveryInterval"] - 1) * interval_length)
        )

    # 15-minute system wide actuals
    elif "TimeEnding" in original_cols:
        ending_time_col_name = "TimeEnding"
        interval_length = pd.Timedelta(minutes=15)

        doc["Interval End"] = pd.to_datetime(
            doc["DeliveryDate"] + " " + doc["TimeEnding"] + ":00",
        )
        doc["Interval End"] = doc["Interval End"].dt.tz_localize(
            ERCOT_TIMEZONE,
            ambiguous=ambiguous,
        )
        doc["Interval Start"] = doc["Interval End"] - interval_length

    else:
        interval_length = pd.Timedelta(hours=1)
        doc["HourBeginning"] = (
            doc[ending_time_col_name]
            .astype(str)
            .str.split(
                ":",
            )
            .str[0]
            .astype(int)
            - 1
        )
        doc["Interval Start"] = pd.to_datetime(doc["DeliveryDate"]) + doc["HourBeginning"].astype(
            int
        ) * pd.Timedelta(hours=1)

    if "TimeEnding" not in original_cols:
        try:
            doc["Interval Start"] = doc["Interval Start"].dt.tz_localize(
                ERCOT_TIMEZONE,
                ambiguous=ambiguous,
                nonexistent=nonexistent,
            )
        except NonExistentTimeError:
            # this handles how ercot does labels the instant
            # of the DST transition differently than
            # pandas does
            doc["Interval Start"] = doc["Interval Start"] + pd.Timedelta(hours=1)
            doc["Interval Start"] = doc["Interval Start"].dt.tz_localize(
                ERCOT_TIMEZONE,
                ambiguous=ambiguous,
            ) - pd.Timedelta(hours=1)
        except pytz.AmbiguousTimeError as e:
            # Handle datasets where HourEnding is like 01:00
            if doc["HourEnding"].min() == "01:00":
                doc["HourEnding"] = (
                    doc["HourEnding"]
                    .astype(str)
                    .str.split(
                        ":",
                    )
                    .str[0]
                    .astype(int)
                )
            # Sometimes ERCOT handles DST end by putting 25 hours in HourEnding
            # which makes IntervalStart where HourEnding >= 3 an hour later than
            # they should be. We correct this by subtracting an hour.
            if doc["HourEnding"].max() != 25:
                raise PriceLayoutError(f"Time parsing error. Did not find HourEnding = 25. {e}") from e
            doc.loc[doc["HourEnding"] >= 3, "Interval Start"] = doc.loc[
                doc["HourEnding"] >= 3,
                "Interval Start",
            ] - pd.Timedelta(hours=1)

            # Now there will be a repeated hour and Pandas can infer
            # the ambiguous value
            doc["Interval Start"] = doc["Interval Start"].dt.tz_localize(
                ERCOT_TIMEZONE,
                ambiguous="infer",
            )

        doc["Interval End"] = doc["Interval Start"] + interval_length

    doc["Time"] = doc["Interval Start"]
    doc = doc.sort_values("Time", ascending=True)

    cols_to_keep = ["Time", "Interval Start", "Interval End", *original_cols]

    doc = doc[cols_to_keep]
    doc = doc.drop(
        columns=["DeliveryDate", ending_time_col_name],
    )

    optional_drop = ["DSTFlag", "DeliveryInterval"]

    for col in optional_drop:
        if col in doc.columns:
            doc = doc.drop(columns=[col])

    return doc


def _ercot_settlement_point_name_and_type(
    df: pd.DataFrame, resource_nodes: Iterable[str] = ()
) -> pd.DataFrame:
    """gridstatus/ercot.py:2434-2493 `_handle_settlement_point_name_and_type`. Upstream downloads
    the settlement point list to name resource nodes; that list is `resource_nodes` here, and
    anything not a hub, zone or DC tie defaults to "Resource Node" either way, as upstream."""
    df = df.rename(
        columns={
            "SettlementPoint": "Location",
            "Settlement Point": "Location",
            "SettlementPointName": "Location",
            "Settlement Point Name": "Location",
        },
    )
    if "Location" not in df.columns:
        raise PriceLayoutError(f"no settlement point column in {list(df.columns)}")

    resource_node = list(resource_nodes)

    # Create boolean masks for each location type
    is_hub = df["Location"].str.startswith("HB_")
    is_load_zone = df["Location"].str.startswith("LZ_")
    is_load_zone_dc_tie = df["Location"].str.startswith("DC_")
    is_resource_node = df["Location"].isin(resource_node)

    # Assign location types based on the boolean masks
    df.loc[is_hub, "Location Type"] = ERCOT_LOCATION_TYPE_HUB
    df.loc[is_load_zone, "Location Type"] = ERCOT_LOCATION_TYPE_ZONE
    df.loc[is_load_zone_dc_tie, "Location Type"] = ERCOT_LOCATION_TYPE_ZONE_DC
    df.loc[is_resource_node, "Location Type"] = ERCOT_LOCATION_TYPE_RESOURCE_NODE
    # If a location type is not found, default to LOCATION_TYPE_RESOURCE_NODE
    df["Location Type"] = df["Location Type"].fillna(ERCOT_LOCATION_TYPE_RESOURCE_NODE)

    # energy weighted only exists in real time data
    # since depends on energy usage
    if "SettlementPointType" in df.columns:
        is_load_zone_energy_weighted = df["SettlementPointType"] == "LZEW"
        is_load_zone_dc_tie_energy_weighted = df["SettlementPointType"] == "LZ_DCEW"

        df.loc[
            is_load_zone_energy_weighted,
            "Location Type",
        ] = ERCOT_LOCATION_TYPE_ZONE_EW
        df.loc[
            is_load_zone_dc_tie_energy_weighted,
            "Location Type",
        ] = ERCOT_LOCATION_TYPE_ZONE_DC_EW

        # append "_EW" to the end of the location name if it is energy weighted
        df.loc[is_load_zone_energy_weighted, "Location"] = (
            df.loc[is_load_zone_energy_weighted, "Location"] + "_EW"
        )

        df.loc[is_load_zone_dc_tie_energy_weighted, "Location"] = (
            df.loc[is_load_zone_dc_tie_energy_weighted, "Location"] + "_EW"
        )

    df["Location"] = df["Location"].astype("string")
    df["Location Type"] = df["Location Type"].astype("category")

    return df


def ercot_finalize_spp(df: pd.DataFrame, market: str, resource_nodes: Iterable[str] = ()) -> pd.DataFrame:
    """gridstatus/ercot.py:2495-2535 `_finalize_spp_df`, without its `filter_lmp_locations` step
    (`locations`/`location_type` were "ALL" in every call this module serves)."""
    df = _ercot_settlement_point_name_and_type(df, resource_nodes)

    df["Market"] = market

    df = df.rename(
        columns={
            "SettlementPointPrice": "SPP",
            "Settlement Point Price": "SPP",
        },
    )
    missing = [c for c in ("Time", "Interval Start", "Interval End", "SPP") if c not in df.columns]
    if missing:
        raise PriceLayoutError(f"columns missing after parse: {missing}")

    df = df[
        [
            "Time",
            "Interval Start",
            "Interval End",
            "Location",
            "Location Type",
            "Market",
            "SPP",
        ]
    ]

    df = df.sort_values(by="Interval Start")
    df = df.reset_index(drop=True)

    return df


def _first_member(content: bytes) -> io.BytesIO:
    """gridstatus/utils.py:211-213 `get_zip_file` over bytes: the archive's first member."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise PriceLayoutError("not a zip archive") from exc
    names = z.namelist()
    if not names:
        raise PriceLayoutError("empty zip archive")
    return io.BytesIO(z.read(names[0]))


def ercot_annual_spp_sheets(
    content: bytes, market: str, keep_sheet: Callable[[str], bool] | None = None
) -> Iterator[tuple[str, pd.DataFrame]]:
    """`ercot_annual_spp` one monthly sheet at a time (change 6, README.md): `(sheet name, frame)`
    for each non-empty sheet, each put through the same steps upstream applies to the concatenated
    workbook. `keep_sheet` names the sheets to read; the others are never parsed (the workbook is
    opened read-only, so a sheet is read only when asked for). Upstream holds every sheet, the
    concatenation and its parsed copy at once: about 400 MB for one real-time year (measured
    2026-10-23), which the worker's 1 GB container cannot spare for four workbooks.

    Equivalence: every step is row-wise once `DSTFlag` is present (both annual workbooks carry
    "Repeated Hour Flag"), so parsing sheet by sheet gives upstream's rows. Upstream's one
    whole-year check (at most one row with neither hour nor interval, dropped) is kept as a running
    count across the sheets read."""
    x = _first_member(content)
    with pd.ExcelFile(x) as book:
        null_rows = 0
        for name in book.sheet_names:
            if keep_sheet is not None and not keep_sheet(str(name)):
                continue
            df = book.parse(name)
            if df.empty:
                continue
            df = df.rename(columns={"Settlement Point Type": "SettlementPointType"})  # change 5
            if market == REAL_TIME_15_MIN:
                count = int(df[["Delivery Hour", "Delivery Interval"]].isnull().all(axis=1).sum())
                null_rows += count
                if null_rows > 1:
                    raise PriceLayoutError(
                        "Parsing error, more than expected null rows found",
                    )
                if count == 1:
                    df = df.dropna(
                        subset=["Delivery Hour", "Delivery Interval"],
                        how="all",
                    )
                df["Delivery Interval"] = df["Delivery Interval"].astype("Int64")
            elif market != DAY_AHEAD_HOURLY:
                raise PriceLayoutError(f"market {market!r} has no annual hub and zone workbook")
            df = ercot_parse_doc(df)
            yield str(name), ercot_finalize_spp(df, market=market)


def ercot_annual_spp(content: bytes, market: str) -> pd.DataFrame:
    """gridstatus/ercot.py:1548-1588 `get_rtm_spp` (market `REAL_TIME_15_MIN`) and 1590-1617
    `get_dam_spp` (`DAY_AHEAD_HOURLY`) over the downloaded zip's bytes: every monthly sheet of the
    NP6-785-ER / NP4-180-ER hub and load-zone workbook, concatenated, parsed and finalised."""
    x = _first_member(content)
    all_sheets = pd.read_excel(x, sheet_name=None)
    df = pd.concat([sheet for sheet in all_sheets.values() if not sheet.empty])
    # Change 5 (README.md): the annual real-time workbook spells the type column "Settlement Point
    # Type", which upstream's energy-weighted labelling (keyed on "SettlementPointType") never sees,
    # so a load zone's LZ and LZEW rows came out under one name. Renamed so they are told apart.
    df = df.rename(columns={"Settlement Point Type": "SettlementPointType"})

    if market == REAL_TIME_15_MIN:
        # fix parsing error where no data is present
        # should only be 1 row per year
        count = df[["Delivery Hour", "Delivery Interval"]].isnull().all(axis=1).sum()
        if count == 1:
            df = df.dropna(
                subset=["Delivery Hour", "Delivery Interval"],
                how="all",
            )
        elif count > 1:
            raise PriceLayoutError(
                "Parsing error, more than expected null rows found",
            )

        df["Delivery Interval"] = df["Delivery Interval"].astype("Int64")
    elif market != DAY_AHEAD_HOURLY:
        raise PriceLayoutError(f"market {market!r} has no annual hub and zone workbook")
    df = ercot_parse_doc(df)
    return ercot_finalize_spp(df, market=market)


# ============================================================================================ CAISO
def caiso_oasis_frame(content: bytes, raw_data: bool = False) -> pd.DataFrame | None:
    """The parse half of gridstatus/caiso/caiso.py:349-470 `CAISO._get_oasis`: a SingleZip
    response's csv members concatenated, `*_GMT` columns as UTC, and (unless `raw_data`) the
    interval columns as `Interval Start`/`Interval End` in US/Pacific with `Time` in front. None
    for an answer that carries no data (OASIS returns an xml error zip), as upstream."""
    # this is when no data is available
    if content[:2] != b"PK" or b".xml" in content:
        return None

    z = zipfile.ZipFile(io.BytesIO(content))

    # parse and concat all files
    dfs = []
    for f in z.namelist():
        df = pd.read_csv(z.open(f))
        dfs.append(df)

    df = pd.concat(dfs)

    # if col ends in _GMT, then try to parse as UTC
    for col in df.columns:
        if col.endswith("_GMT"):
            df[col] = pd.to_datetime(
                df[col],
                utc=True,
            )

    # handle different column names
    # across different datasets
    start_cols = [
        "INTERVALSTARTTIME_GMT",
        "INTERVAL_START_GMT",
        "STARTTIME_GMT",
        "START_DATE_GMT",
    ]
    end_cols = [
        "INTERVALENDTIME_GMT",
        "INTERVAL_END_GMT",
        "ENDTIME_GMT",
        "END_DATE_GMT",
    ]
    start_col = None
    end_col = None
    for col in start_cols:
        if col in df.columns:
            start_col = col
            df = df.sort_values(by=start_col)
            break
    for col in end_cols:
        if col in df.columns:
            end_col = col
            break

    if not raw_data and start_col in df.columns:
        df[start_col] = df[start_col].dt.tz_convert(
            CAISO_TIMEZONE,
        )

        df[end_col] = df[end_col].dt.tz_convert(
            CAISO_TIMEZONE,
        )

        df.rename(
            columns={
                start_col: "Interval Start",
                end_col: "Interval End",
            },
            inplace=True,
        )

        df.insert(0, "Time", df["Interval Start"])

    return df


#: gridstatus/caiso/caiso.py:1522-1532: the value column of each LMP query.
CAISO_PRICE_COLUMN: dict[str, str] = {
    DAY_AHEAD_HOURLY: "MW",
    REAL_TIME_15_MIN: "PRC",
    REAL_TIME_5_MIN: "VALUE",
}


def caiso_lmp(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """The pivot half of gridstatus/caiso/caiso.py:1483-1640 `CAISO._get_lmp`: one row per
    interval and node with `LMP`, `Energy`, `Congestion`, `Loss`, `GHG` and the location type."""
    if market not in CAISO_PRICE_COLUMN:
        raise PriceLayoutError("LMP Market is not supported")
    price_col = CAISO_PRICE_COLUMN[market]
    missing = [
        c for c in ("Time", "Interval Start", "Interval End", "NODE", "LMP_TYPE", price_col) if c not in df
    ]
    if missing:
        raise PriceLayoutError(f"OASIS columns missing: {missing}")

    df = df.pivot_table(
        index=["Time", "Interval Start", "Interval End", "NODE"],
        columns="LMP_TYPE",
        values=price_col,
        aggfunc="first",
    )

    df = df.reset_index().rename(
        columns={
            "NODE": "Location",
            "LMP": "LMP",
            "MCE": "Energy",
            "MCC": "Congestion",
            "MCL": "Loss",
            "MGHG": "GHG",
        },
    )
    for col in ("LMP", "Energy", "Congestion", "Loss", "GHG"):
        if col not in df.columns:
            df[col] = float("nan")

    df["Market"] = market
    df["Location Type"] = "Node"

    # if -APND in location then "APND" Location Type

    df.loc[
        df["Location"].str.endswith("-APND"),
        "Location Type",
    ] = "AP Node"

    df.loc[
        df["Location"].isin(CAISO_TRADING_HUB_LOCATIONS),
        "Location Type",
    ] = "Trading Hub"

    # if starts with "DLAP_" then "DLAP" Location Type
    df.loc[
        df["Location"].str.startswith("DLAP_"),
        "Location Type",
    ] = "DLAP"

    df = df[
        [
            "Time",
            "Interval Start",
            "Interval End",
            "Market",
            "Location",
            "Location Type",
            "LMP",
            "Energy",
            "Congestion",
            "Loss",
            "GHG",
        ]
    ]

    # clean up pivot name in header
    df.columns.name = None

    return df


# ============================================================================================ NYISO
def nyiso_handle_time(df: pd.DataFrame, dataset_name: str, groupby: str | None = None) -> pd.DataFrame:
    """gridstatus/nyiso.py:123-182 `NYISO._handle_time` for the price datasets: `Time Stamp` in
    US/Eastern (DST inferred per location), then `Interval Start`/`Interval End`."""
    if dataset_name not in NYISO_DATASET_INTERVAL_MAP:
        raise PriceLayoutError(f"dataset {dataset_name!r} is not a vendored price dataset")
    time_type, interval_duration_minutes = NYISO_DATASET_INTERVAL_MAP[dataset_name]

    if "Time Stamp" in df.columns:
        time_stamp_col = "Time Stamp"
    elif "Timestamp" in df.columns:
        time_stamp_col = "Timestamp"
    else:
        raise PriceLayoutError(f"no time stamp column in {list(df.columns)}")

    def time_to_datetime(s: pd.Series, dst: Any = "infer") -> pd.Series:
        return pd.to_datetime(s).dt.tz_localize(
            NYISO_TIMEZONE,
            ambiguous=dst,
        )

    if "Time Zone" in df.columns:
        dst = df["Time Zone"] == "EDT"
        df[time_stamp_col] = time_to_datetime(
            df[time_stamp_col],
            dst,
        )

    elif "Name" in df.columns or groupby:
        groupby = groupby or "Name"
        # once we group by name, the time series for each group is no longer ambiguous
        df[time_stamp_col] = df.groupby(groupby, group_keys=False)[time_stamp_col].apply(
            time_to_datetime,
            "infer",
        )
    else:
        df[time_stamp_col] = time_to_datetime(
            df[time_stamp_col],
            "infer",
        )

    df = df.rename(columns={time_stamp_col: "Time"})

    if time_type != "instantaneous" and interval_duration_minutes is not None:
        interval_duration = pd.Timedelta(minutes=interval_duration_minutes)
        if time_type == "start":
            df["Interval Start"] = df["Time"]
            df["Interval End"] = df["Interval Start"] + interval_duration
        elif time_type == "end":
            df["Interval Start"] = df["Time"] - interval_duration
            df["Interval End"] = df["Time"]
            df["Time"] = df["Interval Start"]

        cols = ["Time", "Interval Start", "Interval End"]
        df = df[cols + [c for c in df.columns if c not in cols]]

    return df


def nyiso_lmp(df: pd.DataFrame, market: str, location_type: str = "zone") -> pd.DataFrame:
    """gridstatus/nyiso.py:746-865 `NYISO._process_lmp_data` for the hourly markets
    (`DAY_AHEAD_HOURLY`, `REAL_TIME_HOURLY`). Upstream's five- and fifteen-minute relabelling
    branch fetches the latest interval file and is not taken by the hourly markets."""
    if market not in (DAY_AHEAD_HOURLY, REAL_TIME_HOURLY):
        raise PriceLayoutError(f"market {market!r} needs upstream's network relabelling; not vendored")
    columns = {
        "Name": "Location",
        "LBMP ($/MWHr)": "LMP",
        "Marginal Cost Losses ($/MWHr)": "Loss",
        "Marginal Cost Congestion ($/MWHr)": "Congestion",
        "Marginal Cost Congestion ($/MWH": "Congestion",  # Deal with older data
    }

    df = df.rename(columns=columns)
    missing = [c for c in ("Location", "LMP", "Loss", "Congestion") if c not in df.columns]
    if missing:
        raise PriceLayoutError(f"NYISO columns missing: {missing}")

    # In NYISO raw data, a negative congestion number means a higher LMP. We
    # flip the sign to make it consistent with other ISOs where a negative
    # congestion number means a lower LMP. Thus, LMP = Energy + Loss + Congestion
    # for NYISO, as in other ISOs.
    df["Congestion"] *= -1
    df["Energy"] = (df["LMP"] - df["Loss"] - df["Congestion"]).round(2)
    df["Market"] = market
    df["Location Type"] = location_type.capitalize()

    # We manually update the location type for the reference bus location because
    # it's included in the generator data.
    if NYISO_REFERENCE_BUS_LOCATION in df["Location"].unique():
        df.loc[
            df["Location"] == NYISO_REFERENCE_BUS_LOCATION,
            "Location Type",
        ] = "Reference Bus"

    df = df[
        [
            "Time",
            "Interval Start",
            "Interval End",
            "Market",
            "Location",
            "Location Type",
            "LMP",
            "Energy",
            "Congestion",
            "Loss",
        ]
    ]

    return df[df["Market"] == market].sort_values(["Interval Start", "Location"]).reset_index(drop=True)


# ============================================================================================== SPP
def spp_market_end_to_interval(
    df: pd.DataFrame,
    column: str,
    interval_duration: pd.Timedelta,
    format: str | None = None,
) -> pd.DataFrame:
    """gridstatus/spp.py:1140-1167 `SPP._handle_market_end_to_interval`."""
    if column not in df.columns:
        raise PriceLayoutError(f"no {column!r} column in {list(df.columns)}")
    df = df.rename(
        columns={
            column: "Interval End",
        },
    )

    try:
        df["Interval End"] = pd.to_datetime(
            df["Interval End"],
            utc=True,
            format=format,
        ).dt.tz_convert(SPP_TIMEZONE)
    except (ParserError, ValueError) as exc:
        raise PriceLayoutError(f"interval end unreadable: {exc}") from exc

    df["Interval Start"] = df["Interval End"] - interval_duration

    df["Time"] = df["Interval Start"]

    cols = ["Time", "Interval Start", "Interval End"]
    return df[cols + [c for c in df.columns if c not in cols]]


def spp_finalize_lmp(df: pd.DataFrame, market: str, include_baa: bool = False) -> pd.DataFrame:
    """gridstatus/spp.py:1777-1860 `SPP._finalize_spp_df` for settlement-location files
    (location type `ALL`, so upstream's `filter_lmp_locations` keeps every row)."""
    if market == REAL_TIME_5_MIN:
        interval_duration = pd.Timedelta(minutes=5)
    elif market == DAY_AHEAD_HOURLY:
        interval_duration = pd.Timedelta(hours=1)
    else:
        raise PriceLayoutError(f"Market {market} not supported")

    df = spp_market_end_to_interval(
        df,
        column="GMTIntervalEnd",
        interval_duration=interval_duration,
    )

    df["Market"] = market

    if "Settlement Location" not in df.columns:
        raise PriceLayoutError(f"no 'Settlement Location' column in {list(df.columns)}")
    df["Location"] = df["Settlement Location"]
    df["Location Type"] = (
        df["Location"]
        .map(
            SPP_LMP_HUBS_AND_INTERFACES,
        )
        .fillna(SPP_LOCATION_TYPE_SETTLEMENT_LOCATION)
    )

    df = df.rename(
        columns={
            "Pnode": "PNode",
            "LMP": "LMP",  # for posterity
            "MLC": "Loss",
            "MCC": "Congestion",
            "MEC": "Energy",
        },
    )

    if include_baa and "BAA" not in df.columns:
        df["BAA"] = "SPP"

    # Insert BAA before location if it exists
    cols = [
        "Time",
        "Interval Start",
        "Interval End",
        "Market",
        "Location",
        "Location Type",
        "PNode",
        "LMP",
        "Energy",
        "Congestion",
        "Loss",
    ]

    if "BAA" in df.columns:
        cols.insert(cols.index("Location"), "BAA")

    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise PriceLayoutError(f"SPP columns missing: {missing}")
    df = df[cols]
    df = df.reset_index(drop=True)

    return df.sort_values(["Time", "Location"])
