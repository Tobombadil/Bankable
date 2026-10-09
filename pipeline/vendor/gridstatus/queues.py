# Copyright 2022 James Max Kanter. BSD 3-Clause licence: see LICENSE in this directory.
# Vendored from gridstatus 0.36.0 (https://github.com/gridstatus/gridstatus, PyPI sdist/wheel
# 0.36.0); what was taken, from where, and every change is listed in README.md beside this file.
"""Interconnection-queue parsers for CAISO, ERCOT and NYISO, vendored from gridstatus 0.36.0.

Each function takes the workbook bytes a connector already fetched and returns the frame
`gridstatus.<ISO>().get_interconnection_queue()` returned for the same bytes: same columns, same
order, same dtypes, same rows. Nothing here touches the network (docs/20 §3.1: `parse` never
does). The parsing logic is upstream's, statement for statement; the changes are the input
(bytes instead of an HTTP download), typed signatures, and a `QueueLayoutError` where upstream
used `assert` (so the check survives `python -O`).
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

__all__ = [
    "INTERCONNECTION_COLUMNS",
    "QueueLayoutError",
    "caiso_queue",
    "ercot_queue",
    "format_interconnection_df",
    "nyiso_queue",
]

# gridstatus/base.py:66-71 `InterconnectionQueueStatus` values.
ACTIVE = "Active"
WITHDRAWN = "Withdrawn"
COMPLETED = "Completed"

# gridstatus/base.py:74-92 `_interconnection_columns`.
INTERCONNECTION_COLUMNS: tuple[str, ...] = (
    "Queue ID",
    "Project Name",
    "Interconnecting Entity",
    "County",
    "State",
    "Interconnection Location",
    "Transmission Owner",
    "Generation Type",
    "Capacity (MW)",
    "Summer Capacity (MW)",
    "Winter Capacity (MW)",
    "Queue Date",
    "Status",
    "Proposed Completion Date",
    "Withdrawn Date",
    "Withdrawal Comment",
    "Actual Completion Date",
)


class QueueLayoutError(ValueError):
    """The workbook lacks a column the parser renames or keeps (upstream: AssertionError)."""


def format_interconnection_df(
    queue: pd.DataFrame,
    rename: dict[str, str],
    extra: list[str] | None = None,
    missing: list[str] | None = None,
) -> pd.DataFrame:
    """gridstatus/utils.py:274-298: rename to the common column set, keep `extra`, add `missing`."""
    absent = set(rename) - set(queue.columns)
    if absent:
        raise QueueLayoutError(f"columns to rename not in the workbook: {sorted(absent)}")
    queue = queue.rename(columns=rename)
    columns = list(INTERCONNECTION_COLUMNS)

    if extra:
        for e in extra:
            if e not in queue.columns:
                raise QueueLayoutError(f"Extra column {e} does not exist")
        columns += extra

    if missing:
        for m in missing:
            if m in queue.columns:
                raise QueueLayoutError(f"Missing column {m} already exists")
            queue[m] = None

    return queue[columns].reset_index(drop=True)


# --------------------------------------------------------------------------------------- CAISO
def caiso_queue(content: bytes) -> pd.DataFrame:
    """gridstatus/caiso/caiso.py:1714-1812 `CAISO.get_interconnection_queue`."""
    sheets = pd.read_excel(io.BytesIO(content), skiprows=3, sheet_name=None)

    # remove legend at the bottom
    queued_projects = sheets["Grid GenerationQueue"][:-8]
    completed_projects = sheets["Completed Generation Projects"][:-2]
    withdrawn_projects = sheets["Withdrawn Generation Projects"][:-2].rename(
        columns={"Project Name - Confidential": "Project Name"},
    )

    queue = pd.concat(
        [queued_projects, completed_projects, withdrawn_projects],
    )

    queue = queue.rename(
        columns={
            "Interconnection Request\nReceive Date": "Interconnection Request Receive Date",
            "Actual\nOn-line Date": "Actual On-line Date",
            "Current\nOn-line Date": "Current On-line Date",
            "Interconnection Agreement \nStatus": "Interconnection Agreement Status",
            "Study\nProcess": "Study Process",
            "Proposed\nOn-line Date\n(as filed with IR)": "Proposed On-line Date (as filed with IR)",
            "System Impact Study or \nPhase I Cluster Study": "System Impact Study or Phase I Cluster Study",
            "Facilities Study (FAS) or \nPhase II Cluster Study": (
                "Facilities Study (FAS) or Phase II Cluster Study"
            ),
            "Optional Study\n(OS)": "Optional Study (OS)",
        },
    )

    type_columns = ["Type-1", "Type-2", "Type-3"]
    queue["Generation Type"] = queue[type_columns].apply(
        lambda x: " + ".join(x.dropna()),
        axis=1,
    )

    rename = {
        "Queue Position": "Queue ID",
        "Project Name": "Project Name",
        "Generation Type": "Generation Type",
        "Queue Date": "Queue Date",
        "County": "County",
        "State": "State",
        "Application Status": "Status",
        "Current On-line Date": "Proposed Completion Date",
        "Actual On-line Date": "Actual Completion Date",
        "Reason for Withdrawal": "Withdrawal Comment",
        "Withdrawn Date": "Withdrawn Date",
        "Utility": "Transmission Owner",
        "Station or Transmission Line": "Interconnection Location",
        "Net MWs to Grid": "Capacity (MW)",
    }

    extra_columns = [
        "Type-1",
        "Type-2",
        "Type-3",
        "Fuel-1",
        "Fuel-2",
        "Fuel-3",
        "MW-1",
        "MW-2",
        "MW-3",
        "Interconnection Request Receive Date",
        "Interconnection Agreement Status",
        "Study Process",
        "Proposed On-line Date (as filed with IR)",
        "System Impact Study or Phase I Cluster Study",
        "Facilities Study (FAS) or Phase II Cluster Study",
        "Optional Study (OS)",
        "Full Capacity, Partial or Energy Only (FC/P/EO)",
        "Off-Peak Deliverability and Economic Only",
        "Feasibility Study or Supplemental Review",
    ]

    missing = [
        "Interconnecting Entity",
        "Summer Capacity (MW)",
        "Winter Capacity (MW)",
    ]

    return format_interconnection_df(queue=queue, rename=rename, extra=extra_columns, missing=missing)


# --------------------------------------------------------------------------------------- ERCOT
_ERCOT_FUEL = {
    "BIO": "Biomass",
    "COA": "Coal",
    "GAS": "Gas",
    "GEO": "Geothermal",
    "HYD": "Hydrogen",
    "NUC": "Nuclear",
    "OIL": "Fuel Oil",
    "OTH": "Other",
    "PET": "Petcoke",
    "SOL": "Solar",
    "WAT": "Water",
    "WIN": "Wind",
}

_ERCOT_TECHNOLOGY = {
    "BA": "Battery Energy Storage",
    "CC": "Combined-Cycle",
    "CE": "Compressed Air Energy Storage",
    "CP": "Concentrated Solar Power",
    "EN": "Energy Storage",
    "FC": "Fuel Cell",
    "GT": "Combustion (gas) Turbine, but not part of a Combined-Cycle",
    "HY": "Hydroelectric Turbine",
    "IC": "Internal Combustion Engine, eg. Reciprocating",
    "OT": "Other",
    "PV": "Photovoltaic Solar",
    "ST": "Steam Turbine other than Combined-Cycle",
    "WT": "Wind Turbine",
}


def ercot_queue(content: bytes) -> pd.DataFrame:
    """gridstatus/ercot.py:1515-1643 `Ercot.get_interconnection_queue` (GIS report,
    "Project Details - Large Gen" sheet). `Status` is upstream's: Completed when "IA Signed" is
    non-null, else Active; the lifecycle rules key on the milestone dates instead (docs/22 §3)."""
    # skip rows and handle header
    queue = pd.read_excel(
        io.BytesIO(content),
        sheet_name="Project Details - Large Gen",
        skiprows=30,
    ).iloc[4:]

    queue["State"] = "Texas"
    queue["Queue Date"] = queue["Screening Study Started"]

    queue["Fuel"] = queue["Fuel"].map(_ERCOT_FUEL)
    queue["Technology"] = queue["Technology"].map(_ERCOT_TECHNOLOGY)

    queue["Generation Type"] = queue["Fuel"] + " - " + queue["Technology"]

    queue["Status"] = (
        queue["IA Signed"]
        .isna()
        .map(
            {
                True: ACTIVE,
                False: COMPLETED,
            },
        )
    )

    queue["Actual Completion Date"] = queue["Approved for Synchronization"]

    rename = {
        "INR": "Queue ID",
        "Project Name": "Project Name",
        "Interconnecting Entity": "Interconnecting Entity",
        "Projected COD": "Proposed Completion Date",
        "POI Location": "Interconnection Location",
        "County": "County",
        "State": "State",
        "Capacity (MW)": "Capacity (MW)",
        "Queue Date": "Queue Date",
        "Generation Type": "Generation Type",
        "Actual Completion Date": "Actual Completion Date",
        "Status": "Status",
    }

    extra_columns = [
        "Fuel",
        "Technology",
        "GIM Study Phase",
        "Screening Study Started",
        "Screening Study Complete",
        "FIS Requested",
        "FIS Approved",
        "Economic Study Required",
        "IA Signed",
        "Air Permit",
        "GHG Permit",
        "Water Availability",
        "Meets Planning",
        "Meets All Planning",
        "CDR Reporting Zone",
        "Approved for Energization",
        "Approved for Synchronization",
        "Comment",
    ]

    missing = [
        "Withdrawal Comment",
        "Transmission Owner",
        "Summer Capacity (MW)",
        "Winter Capacity (MW)",
        "Withdrawn Date",
    ]

    return format_interconnection_df(queue=queue, rename=rename, extra=extra_columns, missing=missing)


# --------------------------------------------------------------------------------------- NYISO
_NYISO_TYPE_FUEL = {
    "S": "Solar",
    "ES": "Energy Storage",
    "W": "Wind",
    "AC": "AC Transmission",
    "DC": "DC Transmission",
    "CT": "Combustion Turbine",
    "CC": "Combined Cycle",
    "M": "Methane",
    "H": "Hydro",
    "L": "Load",
    "ST": "Steam Turbine",
    "CC-NG": "Natural Gas",
    "FC": "Fuel Cell",
    "PS": "Pumped Storage",
    "NU": "Nuclear",
    "D": "Dual Fuel",
    "NG": "Natural Gas",
    "Wo": "Wood",
    "F": "Flywheel",
    "CC-D": "Combined Cycle - Dual Fuel",
    "SW": "=Solid Waste",  # sic: upstream's label, kept so the frame is unchanged
    "CT-NG": "Combustion Turbine - Natural Gas",
    "DC/AC": "DC/AC Transmission",
    "CT-D": "Combustion Turbine - Dual Fuel",
    "CS-NG": "Steam Turbine & Combustion Turbine-  Natural Gas",
    "ST-NG": "Steam Turbine - Natural Gas",
}

# The "In Service" sheet has a two-row header; this flattens it onto the other sheets' names.
_NYISO_COMPLETED_COLUMNS: dict[tuple[str, str], str] = {
    ("Queue", "Pos."): "Queue Pos.",
    ("Queue", "Owner/Developer"): "Developer Name",
    ("Queue", "Project Name"): "Project Name",
    ("Date", "of IR"): "Date of IR",
    ("SP", "(MW)"): "SP (MW)",
    ("WP", "(MW)"): "WP (MW)",
    ("Type/", "Fuel"): "Type/ Fuel",
    ("Location", "County"): "County",
    ("Location", "State"): "State",
    ("Z", "Unnamed: 9_level_1"): "Z",
    ("Interconnection", "Point"): "Interconnection Point",
    ("Interconnection", "Utility "): "Utility",
    ("Interconnection", "S"): "S",
    ("Last Update", "Unnamed: 13_level_1"): "Last Updated Date",
    ("Availability", "of Studies"): "Availability of Studies",
    ("SGIA Tender Date", ""): "SGIA Tender Date",
    ("CY Complete Date", ""): "CY Complete Date",
    ("Proposed Initial-Sync Date", ""): "Proposed Initial-Sync Date",
    ("Proposed", " In-Service"): "Proposed In-Service Date",
    ("Proposed", "COD"): "Proposed COD",
    ("Proposed", "COD.1"): "Proposed COD.1",
    ("Proposed", "COD.2"): "Proposed COD.2",
    ("Proposed", "COD.3"): "Proposed COD.3",
    ("Status", ""): "Status",
}


def _flat_completed_name(c: Any) -> str:
    return _NYISO_COMPLETED_COLUMNS[c]


def nyiso_queue(content: bytes) -> pd.DataFrame:
    """gridstatus/nyiso.py:696-901 `NYISO.get_interconnection_queue`: active, cluster, withdrawn,
    cluster-withdrawn and in-service sheets ("Load Projects" is not read)."""
    # Create ExcelFile so the workbook is opened once
    excel_file = pd.ExcelFile(io.BytesIO(content))

    # Drop extra rows at bottom
    active = (
        pd.read_excel(excel_file, sheet_name="Interconnection Queue")
        .dropna(
            subset=["Queue Pos.", "Project Name"],
        )
        .copy()
        # Active projects can have multiple values for "Points of Interconnection"
    ).rename(columns={"Points of Interconnection": "Interconnection Point"})

    cluster_active = (
        pd.read_excel(excel_file, sheet_name=" Cluster Projects")
        .dropna(
            subset=["Queue Pos.", "Project Name"],
        )
        .copy()
    ).rename(columns={"Points of Interconnection": "Interconnection Point"})

    active = pd.concat([active, cluster_active])

    active["Status"] = ACTIVE

    withdrawn = pd.read_excel(excel_file, sheet_name="Withdrawn")
    cluster_withdrawn = pd.read_excel(
        excel_file,
        sheet_name="Cluster Projects-Withdrawn",
    )
    withdrawn = pd.concat([withdrawn, cluster_withdrawn])

    withdrawn["Status"] = WITHDRAWN
    # assume it was withdrawn when last updated
    withdrawn["Withdrawn Date"] = withdrawn["Last Update"]
    withdrawn["Withdrawal Comment"] = None
    withdrawn = withdrawn.rename(columns={"Utility ": "Utility"})

    withdrawn = withdrawn.rename(columns={"Owner/Developer": "Developer Name"})

    # make completed look like the other two sheets
    completed = pd.read_excel(excel_file, sheet_name="In Service", header=[0, 1])
    completed.insert(15, "SGIA Tender Date", None)
    completed.insert(16, "CY Complete Date", None)
    completed.insert(17, "Proposed Initial-Sync Date", None)

    completed["Status"] = COMPLETED

    if "SGIA Tender Date" in active.columns and "SGIA Tender Date" not in completed.columns:
        active = active.drop(columns=["SGIA Tender Date"])
    completed.columns = completed.columns.to_flat_index().map(_flat_completed_name)

    # assume it was finished when last updated
    completed["Actual Completion Date"] = completed["Last Updated Date"]

    dfs = [df for df in [active, withdrawn, completed] if not df.empty and not df.isna().all().all()]
    queue = pd.concat(dfs)

    queue["Type/ Fuel"] = queue["Type/ Fuel"].map(_NYISO_TYPE_FUEL)

    queue["Capacity (MW)"] = (
        queue[["SP (MW)", "WP (MW)"]]
        .replace(
            "TBD",
            0,
        )
        .replace(" ", 0)
        .fillna(0)
        .astype(float)
        .max(axis=1)
    )

    queue["Date of IR"] = pd.to_datetime(queue["Date of IR"])
    queue["Proposed COD"] = pd.to_datetime(
        queue["Proposed COD"],
        errors="coerce",
    )
    queue["Proposed In-Service Date"] = pd.to_datetime(
        queue["Proposed In-Service Date"],
        errors="coerce",
    )
    queue["Proposed Initial-Sync Date"] = pd.to_datetime(
        queue["Proposed Initial-Sync Date"],
        errors="coerce",
    )

    rename = {
        "Queue Pos.": "Queue ID",
        "Project Name": "Project Name",
        "County": "County",
        "State": "State",
        "Developer Name": "Interconnecting Entity",
        "Utility": "Transmission Owner",
        "Interconnection Point": "Interconnection Location",
        "Status": "Status",
        "Date of IR": "Queue Date",
        "Proposed COD": "Proposed Completion Date",
        "Type/ Fuel": "Generation Type",
        "Capacity (MW)": "Capacity (MW)",
        "SP (MW)": "Summer Capacity (MW)",
        "WP (MW)": "Winter Capacity (MW)",
    }

    extra_columns = [
        "Proposed In-Service Date",
        "Proposed Initial-Sync Date",
        "Last Updated Date",
        "Z",
        "S",
        "Availability of Studies",
        "SGIA Tender Date",
    ]

    return format_interconnection_df(queue, rename, extra_columns)
