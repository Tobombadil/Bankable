"""Snapshot diff: pipeline/diff.py."""

import pathlib
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pipeline.diff import EVENT_TYPES, diff_snapshots, perturb


def snap(rows):
    cols = ["record_id", "source_id", "lifecycle_state", "capacity_mw", "proposed_cod"]
    df = pd.DataFrame(rows, columns=cols)
    df["proposed_cod"] = pd.to_datetime(df["proposed_cod"])
    return df


BEFORE = snap(
    [
        ("ercot:1", "ercot", "studied", 100.0, "2027-01-01"),
        ("ercot:2", "ercot", "studied", 100.0, "2027-01-01"),
        ("ercot:3", "ercot", "contracted", 200.0, "2027-06-01"),
        ("caiso:4", "caiso", "studied", 50.0, None),
        ("caiso:5", "caiso", "withdrawn", 50.0, "2026-01-01"),
        ("spp:6", "spp", "studied", None, "2028-01-01"),
    ]
)


def counts(ev):
    return ev["event_type"].value_counts().reindex(EVENT_TYPES, fill_value=0).to_dict()


def test_identical_snapshots_emit_nothing():
    ev = diff_snapshots(BEFORE, BEFORE.copy())
    assert len(ev) == 0
    assert list(ev.columns) == [
        "event_type",
        "record_id",
        "source_id",
        "field",
        "before",
        "after",
        "observed_at",
    ]


def test_new_and_removed():
    after = BEFORE[BEFORE.record_id != "ercot:2"].copy()
    after = pd.concat([after, snap([("ercot:9", "ercot", "studied", 10.0, "2029-01-01")])])
    ev = diff_snapshots(BEFORE, after)
    assert counts(ev) == {
        "new": 1,
        "status_change": 0,
        "capacity_change": 0,
        "cod_change": 0,
        "withdrawn": 0,
        "removed": 1,
    }
    rem = ev[ev.event_type == "removed"].iloc[0]
    assert rem.record_id == "ercot:2" and rem.before == "studied" and rem.after is None
    new = ev[ev.event_type == "new"].iloc[0]
    assert new.record_id == "ercot:9" and new.after == "studied"


def test_status_change_vs_withdrawn():
    after = BEFORE.copy()
    after.loc[after.record_id == "ercot:1", "lifecycle_state"] = "contracted"  # status_change
    after.loc[after.record_id == "ercot:2", "lifecycle_state"] = "withdrawn"  # withdrawn
    after.loc[after.record_id == "ercot:3", "lifecycle_state"] = "cancelled"  # withdrawn (terminal)
    after.loc[after.record_id == "caiso:5", "lifecycle_state"] = (
        "cancelled"  # terminal->terminal = status_change
    )
    ev = diff_snapshots(BEFORE, after)
    assert counts(ev)["withdrawn"] == 2
    assert counts(ev)["status_change"] == 2
    row = ev[(ev.event_type == "status_change") & (ev.record_id == "ercot:1")].iloc[0]
    assert (row.field, row.before, row.after) == ("lifecycle_state", "studied", "contracted")


def test_capacity_change_respects_noise_floor():
    after = BEFORE.copy()
    after.loc[after.record_id == "ercot:1", "capacity_mw"] = 100.4  # < 0.5 MW abs: ignored
    after.loc[after.record_id == "ercot:2", "capacity_mw"] = 120.0  # real change
    after.loc[after.record_id == "ercot:3", "capacity_mw"] = 201.0  # 0.5% rel: ignored
    after.loc[after.record_id == "spp:6", "capacity_mw"] = 75.0  # null -> value: change
    after.loc[after.record_id == "caiso:4", "capacity_mw"] = None  # value -> null: change
    ev = diff_snapshots(BEFORE, after)
    got = set(ev[ev.event_type == "capacity_change"].record_id)
    assert got == {"ercot:2", "spp:6", "caiso:4"}
    r = ev[(ev.event_type == "capacity_change") & (ev.record_id == "ercot:2")].iloc[0]
    assert r.before == "100.0" and r.after == "120.0"


def test_cod_change():
    after = BEFORE.copy()
    after.loc[after.record_id == "ercot:1", "proposed_cod"] = pd.Timestamp("2027-07-01")
    after.loc[after.record_id == "caiso:4", "proposed_cod"] = pd.Timestamp("2028-01-01")  # null -> date
    after.loc[after.record_id == "spp:6", "proposed_cod"] = pd.NaT  # date -> null
    ev = diff_snapshots(BEFORE, after)
    got = {r.record_id: (r.before, r.after) for r in ev[ev.event_type == "cod_change"].itertuples()}
    assert got == {
        "ercot:1": ("2027-01-01", "2027-07-01"),
        "caiso:4": (None, "2028-01-01"),
        "spp:6": ("2028-01-01", None),
    }


def test_one_record_can_emit_several_events():
    after = BEFORE.copy()
    after.loc[after.record_id == "ercot:1", ["lifecycle_state", "capacity_mw"]] = ["contracted", 150.0]
    ev = diff_snapshots(BEFORE, after)
    assert sorted(ev[ev.record_id == "ercot:1"].event_type.astype(str)) == [
        "capacity_change",
        "status_change",
    ]


def test_diff_is_deterministic_and_model_free():
    after = BEFORE.copy()
    after.loc[after.record_id == "ercot:1", "lifecycle_state"] = "withdrawn"
    a = diff_snapshots(BEFORE, after, observed_at="2026-09-12T00:00:00Z")
    b = diff_snapshots(BEFORE, after, observed_at="2026-09-12T00:00:00Z")
    pd.testing.assert_frame_equal(a, b)


def test_perturb_round_trip_counts_are_exact():
    base = pd.concat([BEFORE] * 60, ignore_index=True)
    base["record_id"] = [f"x:{i}" for i in range(len(base))]
    base["lifecycle_state"] = "studied"
    after, expected = perturb(
        base, seed=1, n_new=5, n_status=7, n_capacity=6, n_cod=4, n_withdrawn=3, n_removed=2
    )
    assert counts(diff_snapshots(base, after)) == expected


def test_perturb_refuses_when_too_few_live_rows():
    with pytest.raises(ValueError):
        perturb(BEFORE, n_new=1, n_status=10, n_capacity=10, n_cod=10, n_withdrawn=10, n_removed=10)
