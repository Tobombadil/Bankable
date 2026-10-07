"""Parser changes reach unchanged sources as restatements, never as news (audit 2026-09-30 data
engineer F10), and the source's own status text is news when it moves inside one lifecycle state
(F7, docs/22 §8.3).

Recorded fixtures only; a "parser change" is simulated by bumping a connector's declared version
and wrapping its `normalize`.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pandas as pd

from conftest import FIXTURES
from pipeline.connectors.base import RawSnapshot, _code_digest
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.diff import diff_snapshots
from tests.test_connector_windows import T1 as TED_T1
from tests.test_connector_windows import T2 as TED_T2
from tests.test_connector_windows import FakeTed

NESO = "gb.neso.tec_register"
NESO_URL = "https://api.neso.energy/dataset/tec-register.csv"
T0 = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(days=3)


def _neso(body: bytes, at: dt.datetime) -> RawSnapshot:
    return RawSnapshot(
        content=body, content_type="text/csv", url=NESO_URL, retrieved_at=at, http_status=200, ext="csv"
    )


def _change_parser(monkeypatch, registry, source_id: str, rewrite) -> None:
    """A new parser release: a new declared version and a `normalize` whose output `rewrite` edits."""
    cls = registry.connector_class(source_id)
    original = cls.normalize

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        return rewrite(original(self, rows, raw))

    monkeypatch.setattr(cls, "normalize", normalize)
    monkeypatch.setattr(cls, "parser_version", "9.9.9")


def _relabel(df: pd.DataFrame) -> pd.DataFrame:
    """What a parser fix might do: re-derive capacity and the status text everywhere."""
    out = df.copy()
    out["capacity_mw"] = (pd.to_numeric(out["capacity_mw"], errors="coerce") * 1.5).astype("Float64")
    out["status_raw"] = "Register: " + out["status_raw"].astype("string")
    return out


def test_a_parser_change_reaches_an_unchanged_source_as_a_restatement(tmp_path, monkeypatch, registry):
    body = (FIXTURES / "neso_tec_register.csv").read_bytes()
    st = Store(tmp_path)
    first = run(NESO, registry=registry, store=st, raw=_neso(body, T0))
    assert first.status == "ok"

    _change_parser(monkeypatch, registry, NESO, _relabel)
    second = run(NESO, registry=registry, store=st, raw=_neso(body, T1))

    # Before the fix: same bytes -> `unchanged`, and the new code never reached the source.
    assert second.status == "ok"
    assert second.run["parser_version"].startswith(f"{NESO}@9.9.9+")
    assert second.run["parser_version"] != first.run["parser_version"]
    assert second.run["events_emitted"] == 0, second.events
    summary = second.run["parser_restated"]
    assert summary["restated"] is True and summary["events_suppressed"] > 0
    assert summary["by_type"].get("capacity_change", 0) > 0
    assert any(c["check"] == "parser_restated" and c["level"] == "info" for c in second.run["dq"]["checks"])
    # the stored output carries the new code's values, for the loader to write as field updates
    assert second.records["status_raw"].astype("string").str.startswith("Register: ").all()

    # and the next run under the same code with the same bytes is `unchanged` again
    assert (
        run(NESO, registry=registry, store=st, raw=_neso(body, T1 + dt.timedelta(days=1))).status
        == "unchanged"
    )


def test_a_real_change_in_the_same_run_is_still_news(tmp_path, monkeypatch, registry):
    body = (FIXTURES / "neso_tec_register.csv").read_bytes()
    st = Store(tmp_path)
    assert run(NESO, registry=registry, store=st, raw=_neso(body, T0)).status == "ok"

    _change_parser(monkeypatch, registry, NESO, _relabel)
    text = body.decode("utf-8-sig")
    # A staged row (the record id is project/stage, so the change keeps its identity).
    old_row = "AGS CALDERSIDE 275KV  SUBSTATION,2.00,0.00,200.00,500.00"
    changed = text.replace(old_row, "AGS CALDERSIDE 275KV  SUBSTATION,2.00,0.00,260.00,560.00", 1)
    assert changed != text
    second = run(NESO, registry=registry, store=st, raw=_neso(changed.encode(), T1))
    assert second.status == "ok", second.run.get("hold_reasons")
    ev = second.events
    assert list(ev["event_type"].astype(str)) == ["capacity_change"], ev
    assert (ev.iloc[0]["before"], ev.iloc[0]["after"]) == ("300.0", "390.0")  # both under the new rule


def test_reparse_runs_the_stored_snapshot_without_fetching(tmp_path, monkeypatch, registry):
    body = (FIXTURES / "neso_tec_register.csv").read_bytes()
    st = Store(tmp_path)
    first = run(NESO, registry=registry, store=st, raw=_neso(body, T0))

    _change_parser(monkeypatch, registry, NESO, _relabel)

    def no_fetch(self):  # pragma: no cover - the point is that it is never called
        raise AssertionError("reparse must not fetch")

    monkeypatch.setattr(registry.connector_class(NESO), "fetch", no_fetch)
    again = run(NESO, registry=registry, store=st, reparse=True, now=T1)
    assert again.status == "ok"
    assert again.run["reparse"]["of_run"] == first.run["id"]
    assert again.run["snapshot"]["object_key"] == first.run["snapshot"]["object_key"]
    assert again.run["snapshot"]["retrieved_at"] == first.run["snapshot"]["retrieved_at"]
    assert again.run["events_emitted"] == 0
    assert again.records["status_raw"].astype("string").str.startswith("Register: ").all()


def test_an_incremental_source_is_restated_from_its_stored_rows(tmp_path, monkeypatch, registry):
    api = FakeTed()
    st = Store(tmp_path)
    assert run("eu.ted.api", registry=registry, store=st, http=api, now=TED_T1).status == "ok"

    def retitle(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["status_raw"] = out["status_raw"].astype("string") + "-v2"
        return out

    _change_parser(monkeypatch, registry, "eu.ted.api", retitle)
    second = run("eu.ted.api", registry=registry, store=st, http=api, now=TED_T2)
    assert second.status == "ok", second.run.get("hold_reasons")
    assert second.run["parser_restated"]["restated"] is True
    assert second.run["parser_restated"]["events_suppressed"] > 0
    assert set(second.events["event_type"].astype(str)) <= {"new"}, second.events


def test_the_parser_digest_ignores_comments_and_docstrings(tmp_path):
    a, b, c = (tmp_path / f"{n}.py" for n in "abc")
    a.write_text('"""Doc."""\nX = 1  # one\n\ndef f():\n    """Inner."""\n    return X\n')
    b.write_text('"""Another doc."""\nX = 1\n\ndef f():\n    return X\n')
    c.write_text('"""Doc."""\nX = 2\n\ndef f():\n    return X\n')
    assert _code_digest(a) == _code_digest(b) != _code_digest(c)


def test_every_connector_records_an_effective_parser_version(registry):
    for row in registry.status():
        entry = registry.get(row["id"])
        if not entry.implemented or entry.never_ingest:
            continue
        cls = registry.connector_class(row["id"])
        version = cls.effective_parser_version()
        assert version.startswith(cls.parser_version + "+") and len(version.split("+")[1]) == 8


# ------------------------------------------------------------------------------- F7
def _frame(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "record_id": rid,
                "source_id": "s",
                "lifecycle_state": st,
                "status_raw": raw,
                "capacity_mw": 10.0,
                "proposed_cod": "2028-01-01",
            }
            for rid, st, raw in rows
        ]
    )


def test_the_source_status_text_is_news_inside_one_state():
    before = _frame(
        [
            ("s:1", "under_construction", "(V) Under construction, more than 50 percent complete"),
            ("s:2", "studied", "Phase 1"),
            ("s:3", "studied", "Active "),
        ]
    )
    after = _frame(
        [
            ("s:1", "under_construction", "(TS) Construction complete, but not yet in commercial operation"),
            ("s:2", "contracted", "IA Executed"),
            ("s:3", "studied", "active"),
        ]
    )
    ev = diff_snapshots(before, after)
    assert list(zip(ev["event_type"].astype(str), ev["record_id"], strict=True)) == [
        ("status_change", "s:2"),  # a state change is one event, not two
        ("status_raw_change", "s:1"),  # case and spacing alone (s:3) are not a change
    ]
    row = ev[ev["event_type"] == "status_raw_change"].iloc[0]
    assert row["field"] == "status_raw" and row["after"].startswith("(TS)")


def test_construction_complete_inside_under_construction_is_published(tmp_path):
    """EIA-860M (V) -> (TS): both `under_construction`; 23 such moves were invisible between the
    2026-09-13 and 2026-09-27 files (audit F7)."""
    import openpyxl

    body = (FIXTURES / "eia860m_planned.xlsx").read_bytes()
    wb = openpyxl.load_workbook(io.BytesIO(body))
    ws = wb["Planned"]
    for row in ws.iter_rows():
        cell = next((c for c in row if isinstance(c.value, str) and c.value.startswith("(V) ")), None)
        if cell is not None:
            cell.value = "(TS) Construction complete, but not yet in commercial operation"
            break
    out = io.BytesIO()
    wb.save(out)

    def snap(content: bytes, at: dt.datetime) -> RawSnapshot:
        return RawSnapshot(
            content=content,
            content_type="application/octet-stream",
            url="https://www.eia.gov/x.xlsx",
            retrieved_at=at,
            http_status=200,
            ext="xlsx",
        )

    st = Store(tmp_path)
    assert run("us.eia.860m", store=st, raw=snap(body, T0)).status == "ok"
    second = run("us.eia.860m", store=st, raw=snap(out.getvalue(), T1))
    assert second.status == "ok"
    assert list(second.events["event_type"].astype(str)) == ["status_raw_change"]
