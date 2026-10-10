"""Parser tests for gb.neso.tec_register against a recorded TEC register CSV."""

from __future__ import annotations

import pandas as pd
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
    # the September register writes `1.00`; the key carries the stage number, as October's `1` does
    staged = df[df["source_record_id"].isin(["a0l8e0000010wexAAA/1", "a0l8e0000010wexAAA/2"])]
    assert len(staged) == 2
    assert not df["record_id"].duplicated().any()


def test_unstaged_rows_sharing_a_project_id_get_ordinal_keys(parsed):
    """VPI Immingham on the 2026-09-11 register: a built row and an unstaged +50 MW row under one
    project id. Each gets the project id plus its ordinal among the project's unstaged rows."""
    _, _, rows, df = parsed
    immingham = [i for i, r in enumerate(rows) if r["Project ID"] == "a0l4L0000005im7QAA"]
    assert len(immingham) == 2
    assert list(df.iloc[immingham]["source_record_id"]) == ["a0l4L0000005im7QAA#1", "a0l4L0000005im7QAA#2"]


def test_a_single_row_project_is_keyed_on_its_project_id_alone(parsed):
    _, _, rows, df = parsed
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["Project ID"]] = counts.get(r["Project ID"], 0) + 1
    single = [i for i, r in enumerate(rows) if counts[r["Project ID"]] == 1 and not r["Stage"].strip()]
    assert len(single) >= 30
    assert [df.iloc[i]["source_record_id"] for i in single] == [rows[i]["Project ID"] for i in single]


# ---------------------------------------------------------------- October register (day-first)
OCT_FIXTURE = "neso_tec_register_2026-10.csv"


@pytest.fixture(scope="module")
def october():
    c = connector_for(SOURCE_ID)
    raw = snapshot(OCT_FIXTURE, URL, "text/csv")
    rows = c.parse(raw)
    return rows, c.normalize(rows, raw)


def _cod(df, rows, name):
    i = next(i for i, r in enumerate(rows) if r["Project Name"] == name)
    value = df.iloc[i]["proposed_cod"]
    return None if pd.isna(value) else value.date().isoformat()


def test_october_dates_are_read_day_first(october):
    """The 2026-10-10 register writes `DD/MM/YYYY`; `01/07/2033` is 1 July, not 7 January."""
    rows, df = october
    assert _cod(df, rows, "Banc y Celyn Energy Park") == "2033-07-01"
    assert _cod(df, rows, "Afan Wind Farm and BESS") == "2036-12-06"
    assert _cod(df, rows, "BESS Cilfynydd") == "2029-07-12"
    assert _cod(df, rows, "012 NEP Coventry West") == "2034-10-31"  # day > 12: was right before too
    assert _cod(df, rows, "Abedare") is None  # built, no date


def test_every_dated_october_row_matches_a_strict_day_first_parse(october):
    rows, df = october
    expected = pd.to_datetime(
        pd.Series([r["MW Effective From"] for r in rows]), format="%d/%m/%Y", errors="coerce"
    )
    got = pd.to_datetime(df["proposed_cod"], errors="coerce")
    assert expected.notna().sum() == 18  # 22 rows, four built rows with no date
    assert (got.isna() == expected.isna()).all()
    assert (got[expected.notna()] == expected[expected.notna()]).all()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01/07/2033", "2033-07-01"),
        ("1/7/2033", "2033-07-01"),
        ("2033-07-01", "2033-07-01"),
        (" 31/10/2034 ", "2034-10-31"),
        ("31/02/2030", None),  # impossible: never re-read month-first or rolled over
        ("07-01-2033", None),  # an unknown shape is not guessed at
        ("", None),
        (None, None),
    ],
)
def test_effective_date_reads_only_the_registers_two_shapes(value, expected):
    from pipeline.connectors.gb_neso_tec_register.connector import effective_date

    got = effective_date(value)
    assert (None if pd.isna(got) else got.date().isoformat()) == expected


def test_october_keys(october):
    rows, df = october
    keys = dict(
        zip([r["Project Name"] + "|" + r["Stage"] for r in rows], df["source_record_id"], strict=True)
    )
    assert keys["Banc y Celyn Energy Park|"] == "a0l4L0000005ia8QAA"
    assert keys["Immingham|2"] == "a0l4L0000005im7QAA/2"
    assert keys["Immingham|3"] == "a0l4L0000005im7QAA/3"
    assert keys["Cruachan U3&4 Replacement|2"] == "a0l4L0000005inkQAA/2"  # a lone staged row keeps its stage
    assert [keys[f"MeyGen Tidal|{n}"] for n in range(1, 6)] == [
        f"a0l4L0000005isuQAA/{n}" for n in range(1, 6)
    ]
    assert not df["record_id"].duplicated().any()


@pytest.mark.parametrize(
    ("cell", "token"), [("1.00", "1"), ("1", "1"), (" 2 ", "2"), ("1.5", "1.5"), ("", "")]
)
def test_stage_token_ignores_the_registers_number_format(cell, token):
    from pipeline.connectors.gb_neso_tec_register.connector import stage_token

    assert stage_token(cell) == token


def test_a_project_without_an_id_is_keyed_on_its_stable_fields():
    from pipeline.connectors.gb_neso_tec_register.connector import record_keys

    row = {"Project ID": "", "Project Name": "X", "Customer Name": "Y", "Connection Site": "Z", "Stage": ""}
    moved = dict(row, **{"MW Effective From": "01/01/2031", "Project Status": "Built"})
    assert record_keys([row]) == record_keys([moved])
    assert record_keys([row])[0].startswith("noid-h")


# ---------------------------------------------------------------- change events on the new keys
def _october_run(store, text: str, day: int):
    import datetime as dt

    from pipeline.connectors.runner import run

    raw = snapshot(OCT_FIXTURE, URL, "text/csv", retrieved_at=dt.datetime(2026, 10, day, tzinfo=dt.UTC))
    raw.content = b"\xef\xbb\xbf" + text.encode("utf-8")
    return run(SOURCE_ID, store=store, raw=raw)


def _october_text() -> str:
    from conftest import FIXTURES

    return (FIXTURES / OCT_FIXTURE).read_bytes().decode("utf-8-sig")


def test_a_date_slip_and_a_status_change_are_changes_not_new_and_removed(tmp_path):
    """Review 2026-10-10 §2.7 item 2: on the hashed keys a slipped date was a `new` plus a
    `removed`. On the project-id key it is one `cod_change`; a status move is one `status_change`."""
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    text = _october_text()
    assert _october_run(store, text, 10).status == "ok"
    slipped = text.replace(",01/07/2033,Scoping,", ",01/07/2034,Scoping,", 1)  # Banc y Celyn slips a year
    slipped = slipped.replace(
        ",03/04/2025,Consents Approved,", ",03/04/2025,Under Construction/Commissioning,", 1
    )
    assert slipped.count("01/07/2034") == 1 and "Under Construction/Commissioning" in slipped
    result = _october_run(store, slipped, 13)
    assert result.status == "ok", result.run.get("error")
    ev = result.events
    assert ev is not None
    assert set(ev["event_type"]) == {"cod_change", "status_change"}, ev.to_dict("records")
    cod = ev[ev["event_type"] == "cod_change"].iloc[0]
    assert cod["record_id"] == "gb.neso.tec_register:a0l4L0000005ia8QAA"
    assert (cod["before"], cod["after"]) == ("2033-07-01", "2034-07-01")
    assert result.run["rows_new"] == 0 and result.run["rows_gone"] == 0


def test_the_old_keys_and_dates_are_restated_without_events(tmp_path, monkeypatch):
    """A store whose NESO frame was written by parser 1.0.0 (hashed keys, month-first dates), then
    `run --reparse` under 2.0.0 from the same stored bytes: the frame is restated under the new
    keys and dates, and the run emits nothing (docs/61 §4a)."""
    from pipeline.connectors import runner
    from pipeline.connectors.base import content_hash
    from pipeline.connectors.canonical import to_date
    from pipeline.connectors.gb_neso_tec_register import connector as neso
    from pipeline.connectors.registry import Registry
    from pipeline.connectors.store import Store

    def old_keys(rows):
        return [
            f"{r['Project ID']}/{r['Stage'].strip()}"
            if r["Stage"].strip()
            else f"{r['Project ID']}/"
            + content_hash(
                r.get("MW Effective From"),
                r.get("Project Status"),
                r.get("MW Connected"),
                r.get("MW Increase / Decrease"),
            )
            for r in rows
        ]

    store, registry = Store(tmp_path), Registry()
    monkeypatch.setattr(neso, "record_keys", old_keys)
    monkeypatch.setattr(neso, "effective_date", to_date)
    monkeypatch.setattr(neso.Connector, "parser_version", "1.0.0")
    first = _october_run(store, _october_text(), 10)
    assert first.status == "ok"
    old = first.records
    assert old is not None
    assert old["source_record_id"].str.contains("/h").sum() == 10
    assert (pd.to_datetime(old["proposed_cod"]).dt.strftime("%Y-%m-%d") == "2033-01-07").any()
    monkeypatch.undo()

    assert runner.reparse_skip_reason(SOURCE_ID, registry=registry, store=store) is None
    restated = runner.run(SOURCE_ID, registry=registry, store=store, reparse=True)
    assert restated.status == "ok", restated.run.get("error")
    assert restated.run["events_emitted"] == 0
    assert (
        restated.run["rows_new"] == 0 and restated.run["rows_gone"] == 0 and restated.run["rows_changed"] == 0
    )
    summary = restated.run["parser_restated"]
    assert summary["restated"] is True and summary["to"].startswith(f"{SOURCE_ID}@2.0.0+")
    # what the restatement kept out of the feed: 10 re-keyed rows as new + removed, plus the
    # month-first dates of the staged rows whose keys did not move
    assert summary["by_type"]["new"] == 10 and summary["by_type"]["removed"] == 10
    assert summary["by_type"]["cod_change"] == 5
    df = restated.records
    assert df is not None
    assert "gb.neso.tec_register:a0l4L0000005ia8QAA" in set(df["record_id"])
    assert runner.reparse_skip_reason(SOURCE_ID, registry=registry, store=store) == "up_to_date"


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
