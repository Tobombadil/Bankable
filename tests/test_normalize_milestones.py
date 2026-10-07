"""`pipeline.normalize` milestone columns per source (docs/22 §3.2)."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from pipeline.normalize import load_status_map, milestones_from_raw, normalize_iso


def _frame(**cols: list[object]) -> pd.DataFrame:
    base = {"Queue ID": ["1"], "Project Name": ["X"], "Status": ["ACTIVE"], "Capacity (MW)": [10.0]}
    return pd.DataFrame({**base, **cols})


def test_caiso_states_process_withdrawal_and_completion() -> None:
    out = normalize_iso(
        _frame(
            Status=["WITHDRAWN"],
            **{
                "Queue Date": ["2009-01-15"],
                "Study Process": ["C14"],
                "Withdrawn Date": ["2013-03-20"],
                "Actual Completion Date": [None],
            },
        ),
        "caiso",
        load_status_map(),
        "2026-10-07T00:00:00Z",
    )
    row = out.iloc[0]
    assert row["study_phase"] == "C14"
    assert row["withdrawn_date"] == pd.Timestamp("2013-03-20")
    assert pd.isna(row["actual_cod"]) and pd.isna(row["ia_date"])


def test_ercot_states_phase_ia_and_synchronisation() -> None:
    out = normalize_iso(
        _frame(
            Status=["Completed"],
            **{
                "GIM Study Phase": ["SS Completed, FIS Completed, IA"],
                "IA Signed": ["2024-04-10"],
                "Approved for Synchronization": ["2026-04-06"],
                "Actual Completion Date": [None],
            },
        ),
        "ercot",
        load_status_map(),
        "2026-10-07T00:00:00Z",
    )
    row = out.iloc[0]
    assert row["lifecycle_state"] == "built"
    assert row["ia_date"] == pd.Timestamp("2024-04-10")
    assert row["actual_cod"] == pd.Timestamp("2026-04-06")  # synchronisation when no completion date
    assert pd.isna(row["withdrawn_date"])


def test_milestones_from_raw_and_a_source_that_states_none() -> None:
    raw = {
        "Queue Date": "2018-04-25T00:00:00",
        "Withdrawn Date": "5/31/21",
        "Availability of Studies": "SRIS, FS",
    }
    assert milestones_from_raw("us.iso.nyiso.gen_queue", raw) == {
        "queue_date": dt.date(2018, 4, 25),
        "study_phase": "SRIS, FS",
        "withdrawn_date": dt.date(2021, 5, 31),
    }
    assert milestones_from_raw("us.eia.860m", {"Queue Date": "2018-01-01"}) == {}
