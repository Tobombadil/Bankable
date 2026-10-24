"""How the lane's tag fixes reach the notices production already holds (docs/28 lane F3).

The opportunity connectors' parser digest covers `pipeline/connectors/opportunity.py` and each
connector's own modules (`Connector.parser_digest`), so this lane's code moves every opportunity
source's effective parser version. On each source's next run the runner restates the last
promoted output under the new code (an incremental source such as TED from each stored row's own
`raw`), carries every stored row forward re-tagged, emits no news for the re-tagging, and queues
the load that writes the new tags onto the stored records (`services.ingest.loader`). Recorded
fixtures only."""

from __future__ import annotations

from typing import Any

import pandas as pd

from pipeline.connectors.eu_ted_api import connector as ted
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from tests.test_connector_windows import T1, T2, FakeTed


def _old_technologies(title: Any, codes: list[str]) -> list[str]:
    """The base commit's CPV rule that docs/28 B-3 fixed: any `093` code was solar PV."""
    return ["solar_pv"] if any(str(c).startswith("093") for c in codes) else []


def _tokens(df: pd.DataFrame) -> list[list[str]]:
    return [[t for t in str(v or "").split("|") if t] for v in df["technologies"]]


def test_stored_ted_notices_are_retagged_by_the_next_run_as_a_restatement(tmp_path, monkeypatch, registry):
    api = FakeTed()  # every notice: CPV 09310000 Electricity, which the old rule read as solar PV
    st = Store(tmp_path)
    monkeypatch.setattr(ted, "technologies", _old_technologies)
    monkeypatch.setattr(registry.connector_class("eu.ted.api"), "parser_version", "0.9.0")
    first = run("eu.ted.api", registry=registry, store=st, http=api, now=T1)
    assert first.status == "ok"
    assert all(tokens == ["solar_pv"] for tokens in _tokens(first.records))

    monkeypatch.undo()  # the lane's code, at its own declared version
    second = run("eu.ted.api", registry=registry, store=st, http=api, now=T2)
    assert second.status == "ok", second.run.get("hold_reasons")
    assert second.run["parser_restated"]["restated"] is True
    carried = second.records[second.records["record_id"].isin(first.records["record_id"])]
    assert len(carried) == len(first.records)  # every stored notice is carried forward ...
    assert all("solar_pv" not in tokens for tokens in _tokens(carried))  # ... re-tagged
    # and the re-tagging is a restatement: the only events are the window's new notices.
    assert set(second.events["event_type"].astype(str)) <= {"new"}, second.events
