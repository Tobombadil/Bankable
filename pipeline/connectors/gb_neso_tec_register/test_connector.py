"""Parser tests for gb.neso.tec_register against a recorded TEC register CSV."""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError

SOURCE_ID = "gb.neso.tec_register"
URL = "https://api.neso.energy/dataset/resource/tec-register.csv"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("neso_tec_register.csv", URL, "text/csv")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_bom_prefixed_csv_parses(parsed):
    _, _, rows, _ = parsed
    assert {"Project Name", "Customer Name", "Project ID", "Project Status", "Plant Type"} <= set(rows[0])


def test_status_map_is_the_per_connector_one(parsed):
    c, _, _, df = parsed
    assert c.status_map_path is not None and c.status_map_path.name == "status_map.yaml"
    assert set(df["lifecycle_state"]) <= {"filed", "studied", "permitted", "under_construction", "built"}
    assert df["status_rule"].str.startswith("neso_tec.").all()


def test_staged_projects_key_on_project_id_plus_stage(parsed):
    _, _, _, df = parsed
    staged = df[df["source_record_id"].str.contains("/1.00|/2.00", regex=True)]
    assert len(staged) >= 2
    assert not df["record_id"].duplicated().any()


def test_unstaged_rows_sharing_a_project_id_get_distinct_hashed_keys(parsed):
    _, _, rows, df = parsed
    immingham = [i for i, r in enumerate(rows) if r["Project ID"] == "a0l4L0000005im7QAA"]
    assert len(immingham) == 2
    keys = set(df.iloc[immingham]["source_record_id"])
    assert len(keys) == 2


def test_multi_technology_plant_types_classify(parsed):
    _, _, _, df = parsed
    assert df["technology"].notna().all()
    assert set(df["iso"]) == {"NESO"}


def test_html_instead_of_csv_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("neso_tec_register.csv", URL, "text/csv")
    raw.content = b"<html><head><title>302 Found</title></head></html>"
    with pytest.raises(ParseError):
        c.parse(raw)


# ---------------------------------------------------------------- stage capacity (audit F3)
def _mw(df, rows, project_id):
    idx = [i for i, r in enumerate(rows) if r["Project ID"] == project_id]
    return sorted(float(v) for v in df.iloc[idx]["capacity_mw"])


def test_a_staged_project_counts_each_stage_once(parsed):
    """AGS Calderside: stage 1 +300 MW, stage 2 +200 MW, cumulative 300 then 500. The stages are
    two proposals, so their capacities must sum to the project's 500 MW, not 800."""
    _, _, rows, df = parsed
    assert _mw(df, rows, "a0l8e0000010wexAAA") == [200.0, 300.0]


def test_an_unstaged_increase_on_a_built_project_is_only_the_increase(parsed):
    """VPI Immingham: a built row (1,268 MW connected) and a +50 MW row whose cumulative figure
    repeats the built 1,268 MW."""
    _, _, rows, df = parsed
    assert _mw(df, rows, "a0l4L0000005im7QAA") == [50.0, 1268.0]


def test_a_built_stage_keeps_its_connected_mw():
    """Arecleoch on the 2026-09-13 register: stage 1 is built (114 MW connected, +0), stage 2 adds
    216 MW (cumulative 330). The increase alone would make stage 1 zero."""
    c = connector_for(SOURCE_ID)
    raw = snapshot("neso_tec_register.csv", URL, "text/csv")
    header = raw.content.decode("utf-8-sig").splitlines()[0]
    lines = [
        header,
        "Arecleoch,SCOTTISHPOWER,Arecleoch 132kV,1.00,114.00,0.00,114.00,,Built,Directly Connected,SPT,"
        "Wind Onshore,a0l4L0000005itIQAQ,PRO-1,",
        "Arecleoch,SCOTTISHPOWER,Arecleoch 132kV,2.00,0.00,216.00,330.00,2032-06-15,Scoping,"
        "Directly Connected,SPT,Wind Onshore,a0l4L0000005itIQAQ,PRO-1,",
    ]
    raw.content = ("\n".join(lines) + "\n").encode("utf-8")
    rows = c.parse(raw)
    df = c.normalize(rows, raw)
    assert list(df["capacity_mw"]) == [114.0, 216.0]
    assert [r["Cumulative Total Capacity (MW)"] for r in rows] == ["114.00", "330.00"]


def test_restate_capacity_reads_a_frame_stored_under_the_cumulative_rule(parsed):
    c, _, rows, df = parsed
    old = df.copy()
    old["capacity_mw"] = [float(r["Cumulative Total Capacity (MW)"]) for r in rows]
    restated = c.restate_capacity(old)
    assert restated is not None
    assert list(restated.astype(float)) == list(df["capacity_mw"].astype(float))


def test_the_capacity_correction_is_restated_not_published(tmp_path, monkeypatch):
    """A store whose last NESO frame was written under the cumulative rule, then a run under the
    stage rule: the two moved rows are counted as restated and no capacity_change is emitted for
    them, while a real change in the same run still is."""
    import datetime as dt

    from pipeline.connectors.gb_neso_tec_register import connector as neso
    from pipeline.connectors.registry import Registry
    from pipeline.connectors.runner import run
    from pipeline.connectors.store import Store

    store, registry = Store(tmp_path), Registry()
    first = snapshot(
        "neso_tec_register.csv", URL, "text/csv", retrieved_at=dt.datetime(2026, 9, 13, tzinfo=dt.UTC)
    )
    monkeypatch.setattr(
        neso, "stage_capacity_mw", lambda r: float(r["Cumulative Total Capacity (MW)"] or 0) or None
    )
    assert run(SOURCE_ID, registry=registry, store=store, raw=first).status == "ok"
    monkeypatch.undo()

    second = snapshot(
        "neso_tec_register.csv", URL, "text/csv", retrieved_at=dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
    )
    text = second.content.decode("utf-8-sig")
    # One real change: AGS Calderside's stage 2 grows from +200 MW to +250 MW.
    changed = text.replace(",2.00,0.00,200.00,500.00,", ",2.00,0.00,250.00,550.00,", 1)
    assert changed != text
    second.content = changed.encode("utf-8")
    result = run(SOURCE_ID, registry=registry, store=store, raw=second)
    assert result.status == "ok", result.run.get("error")
    assert result.run["reclassified"]["capacity_rows"] == 2  # AGS Calderside stage 2, Immingham +50
    assert result.dq is not None
    assert any(c.check == "capacity_restated" for c in result.dq.checks)
    ev = result.events
    assert ev is not None
    cap = ev[ev["event_type"] == "capacity_change"]
    assert list(zip(cap["before"], cap["after"], strict=True)) == [("200.0", "250.0")]
