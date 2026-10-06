"""us.eia.860m.retirements against a recorded trim of the August 2026 EIA-860M workbook, and the
runner diff it feeds: a first run emits no events, a month in which planned retirements move emits
exactly those changes, and a status-map correction is a reclassification, not an event."""

from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
from collections.abc import Callable
from typing import Any

import openpyxl
import pandas as pd
import pytest

from conftest import connector_for, fixture_path, snapshot
from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.context.retirements import RETIRED_STATUS_RAW, workbook_as_of

SOURCE_ID = "us.eia.860m.retirements"
FIXTURE = "eia860m_operating_retired.xlsx"
URL = "https://www.eia.gov/electricity/data/eia860m/xls/august_generator2026.xlsx"
MONTH1 = dt.datetime(2026, 9, 27, 15, 15, tzinfo=dt.UTC)
MONTH2 = dt.datetime(2026, 10, 27, 15, 15, tzinfo=dt.UTC)


# ------------------------------------------------------------------------- month-two workbook
def _header(ws: Any) -> list[str]:
    return [str(c.value).strip() if c.value is not None else "" for c in ws[3]]


def _find(ws: Any, plant_id: int, generator_id: str) -> int:
    cols = _header(ws)
    pid_i, gid_i = cols.index("Plant ID"), cols.index("Generator ID")
    for r in range(4, ws.max_row + 1):
        pid, gid = ws.cell(r, pid_i + 1).value, ws.cell(r, gid_i + 1).value
        try:
            if int(float(pid)) == plant_id and str(gid).replace(".0", "") == generator_id:
                return r
        except (TypeError, ValueError):
            continue
    raise KeyError((plant_id, generator_id))


def _set(ws: Any, row: int, column: str, value: Any) -> None:
    ws.cell(row, _header(ws).index(column) + 1).value = value


def _retire(wb: Any, plant_id: int, generator_id: str, year: int, month: int) -> None:
    """Move one generator from the Operating sheet to the Retired sheet, as EIA does."""
    op, ret = wb["Operating"], wb["Retired"]
    r = _find(op, plant_id, generator_id)
    values = dict(zip(_header(op), (c.value for c in op[r]), strict=False))
    op.delete_rows(r)
    new = [values.get(col) for col in _header(ret)]
    cols = _header(ret)
    new[cols.index("Retirement Year")] = year
    new[cols.index("Retirement Month")] = month
    ret.insert_rows(4)
    for i, v in enumerate(new, start=1):
        ret.cell(4, i).value = v


def month_two(mutate: Callable[[Any], None] | None = None) -> bytes:
    """The recorded workbook as the next month might publish it, edited in memory only:

    * Cardinal unit 3 (OH): planned retirement moves from December 2028 to June 2030;
    * Sand Point unit 1 (AK, standby): gains a planned retirement, June 2027;
    * Rockport unit 1 (IN): leaves the Operating sheet for the Retired sheet, September 2026;
    * La Cygne unit 2 (KS): its planned retirement (2039) is withdrawn.
    """
    wb = openpyxl.load_workbook(fixture_path(FIXTURE))
    op = wb["Operating"]
    r = _find(op, 2828, "3")
    _set(op, r, "Planned Retirement Year", 2030)
    _set(op, r, "Planned Retirement Month", 6)
    r = _find(op, 1, "1")
    _set(op, r, "Planned Retirement Year", 2027)
    _set(op, r, "Planned Retirement Month", 6)
    r = _find(op, 1241, "2")
    _set(op, r, "Planned Retirement Year", None)
    _set(op, r, "Planned Retirement Month", None)
    _retire(wb, 6166, "1", 2026, 9)
    if mutate is not None:
        mutate(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def raw_of(content: bytes, retrieved_at: dt.datetime) -> RawSnapshot:
    return RawSnapshot(
        content=content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        url=URL,
        retrieved_at=retrieved_at,
        http_status=200,
        ext="xlsx",
    )


# ----------------------------------------------------------------------------------- parsing
@pytest.fixture(scope="module")
def parsed() -> tuple[Any, list[dict[str, Any]], pd.DataFrame]:
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, URL, retrieved_at=MONTH1)
    rows = c.parse(raw)
    return c, rows, c.normalize(rows, raw)


def by_id(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    return {str(r["source_record_id"]): r for r in df.to_dict("records")}


def test_both_sheets_parse_and_plant_less_rows_are_dropped(parsed: Any) -> None:
    _, rows, df = parsed
    sheets = pd.Series([r["_sheet"] for r in rows]).value_counts().to_dict()
    # 23 Operating rows; 8 Retired rows of which 2 are the plant-id-less nuclear appendix.
    assert sheets == {"Operating": 23, "Retired": 6}
    assert not any(str(r.get("Plant Name")) in ("Bonus", "Shoreham") for r in rows)
    assert all(r["_as_of"] == "2026-08" for r in rows)
    assert len(df) == 29 and not df["record_id"].duplicated().any()


def test_record_id_is_plant_and_generator_and_survives_a_move_between_sheets(parsed: Any) -> None:
    _, _, df = parsed
    rows = by_id(df)
    # A numeric Generator ID reads the same on both sheets ("1", never "1.0").
    assert {"869-1", "869-2", "869-3", "6155-1", "3122-3", "1-5.1", "1-WT1"} <= set(rows)
    assert rows["869-1"]["document_type"] == "Retired" and rows["869-2"]["document_type"] == "Operating"
    assert (df["record_id"] == SOURCE_ID + ":" + df["source_record_id"]).all()


def test_generator_states_follow_the_asset_status_map(parsed: Any) -> None:
    _, _, df = parsed
    rows = by_id(df)
    assert rows["1-2"]["lifecycle_state"] == "operating"
    assert rows["1-1"]["lifecycle_state"] == "standby"  # (SB)
    assert rows["1-WT1"]["lifecycle_state"] == "standby"  # (OS)
    assert rows["145-HM3"]["lifecycle_state"] == "standby"  # (OA)
    assert rows["6166-1"]["lifecycle_state"] == "retiring"
    assert rows["6166-1"]["status_rule"] == "eia860m_generators.planned_retirement"
    assert rows["2364-2"]["lifecycle_state"] == "retiring"  # out of service AND scheduled
    assert rows["6155-1"]["lifecycle_state"] == "retired"
    assert rows["6155-1"]["status_raw"] == RETIRED_STATUS_RAW
    assert set(df["lifecycle_state"]) <= {"operating", "standby", "retiring", "retired"}
    assert not df["status_rule"].str.endswith(".unmapped").any()


def test_retirement_dates_keep_the_precision_eia_states(parsed: Any) -> None:
    _, _, df = parsed
    rows = by_id(df)
    assert rows["2828-3"]["proposed_cod"] == "2028-12-01"
    assert json.loads(rows["2828-3"]["identifiers"])["retirement"] == "2028-12"
    # La Cygne's planned retirement states a year and no month.
    assert rows["1241-1"]["proposed_cod"] == "2032-01-01"
    assert json.loads(rows["1241-1"]["identifiers"])["retirement"] == "2032"
    assert rows["3122-3"]["proposed_cod"] == "2023-07-01"
    assert rows["2828-1"]["proposed_cod"] is None
    assert rows["6166-1"]["capacity_mw"] == 1300.0


def test_provenance_quartet_on_every_row(parsed: Any) -> None:
    _, _, df = parsed
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert set(df["source_id"]) == {SOURCE_ID}


def test_html_placeholder_is_a_parse_error() -> None:
    c = connector_for(SOURCE_ID)
    with pytest.raises(ParseError):
        c.parse(raw_of(b"<html>coming soon</html>", MONTH1))


def test_as_of_is_read_from_the_title_row() -> None:
    assert workbook_as_of("Inventory of Retired Generators as of August 2026") == "2026-08"
    assert workbook_as_of("no date here") is None


def test_restate_recomputes_the_stored_state_from_raw(parsed: Any) -> None:
    c, _, df = parsed
    restated = c.restate_status(df)
    assert restated is not None
    assert (restated["lifecycle_state"] == df["lifecycle_state"]).all()
    assert (restated.index == df.index).all()


# ---------------------------------------------------------------------- runner diff, end to end
@pytest.fixture()
def store(tmp_path: pathlib.Path) -> Store:
    return Store(tmp_path)


def test_first_run_emits_no_events_however_many_retirements_it_holds(
    registry: Registry, store: Store
) -> None:
    first = run(SOURCE_ID, registry=registry, store=store, raw=snapshot(FIXTURE, URL, retrieved_at=MONTH1))
    assert first.status == "ok", first.run.get("error")
    assert first.run["events_emitted"] == 0
    assert "events" not in first.paths
    assert first.run["rows_new"] == 29


def test_a_month_of_retirement_changes_is_exactly_those_events(registry: Registry, store: Store) -> None:
    run(SOURCE_ID, registry=registry, store=store, raw=snapshot(FIXTURE, URL, retrieved_at=MONTH1))
    second = run(SOURCE_ID, registry=registry, store=store, raw=raw_of(month_two(), MONTH2))
    assert second.status == "ok", second.run.get("error")
    ev = second.events
    assert ev is not None
    got = {
        (r["record_id"].split(":")[1], r["event_type"], r["before"], r["after"])
        for r in ev.to_dict("records")
    }
    assert got == {
        ("2828-3", "cod_change", "2028-12-01", "2030-06-01"),
        ("1-1", "status_change", "standby", "retiring"),
        ("1-1", "cod_change", None, "2027-06-01"),
        ("6166-1", "status_change", "retiring", "retired"),
        ("6166-1", "cod_change", "2028-12-01", "2026-09-01"),
        ("1241-2", "status_change", "retiring", "operating"),
        ("1241-2", "cod_change", "2039-01-01", None),
    }
    assert second.run["rows_reclassified"] == 0


def test_a_status_map_correction_is_a_reclassification_not_an_event(
    registry: Registry, store: Store, tmp_path: pathlib.Path
) -> None:
    """FX1 on this source: the same workbook read under a map that calls (OS) `operating` instead
    of `standby` moves stored rows without a single status_change event."""
    run(SOURCE_ID, registry=registry, store=store, raw=snapshot(FIXTURE, URL, retrieved_at=MONTH1))
    cls = registry.connector_class(SOURCE_ID)
    corrected = tmp_path / "asset_status_map.yaml"
    text = pathlib.Path(str(cls.status_map_path)).read_text(encoding="utf-8")
    corrected.write_text(
        text.replace(
            '"(OS) Out of service and NOT expected to return to service in next calendar year": standby',
            '"(OS) Out of service and NOT expected to return to service in next calendar year": operating',
        ),
        encoding="utf-8",
    )
    original = cls.status_map_path
    try:
        cls.status_map_path = corrected
        # One unrelated real change so the bytes differ and the run is not short-circuited.
        content = month_two(lambda wb: None)
        second = run(SOURCE_ID, registry=registry, store=store, raw=raw_of(content, MONTH2))
    finally:
        cls.status_map_path = original
    assert second.status == "ok", second.run.get("error")
    assert second.run["rows_reclassified"] == 2  # Sand Point WT1, WT2
    ev = second.events
    assert ev is not None
    moved = ev[(ev["event_type"] == "status_change")]["record_id"].str.split(":").str[1]
    assert not set(moved) & {"1-WT1", "1-WT2"}
