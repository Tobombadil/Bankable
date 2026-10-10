"""The churn gate (`pipeline/connectors/dq.py`, "Churn"; docs/51 §2.7 item 2, 2026-10-10).

A source that re-keys its rows shares no keys with the previous frame: `field_nulled` has nothing to
compare, every other gate passes, and the diff would publish one `removed` and one `new` event per
row. The gate holds such a run, through the ordinary hold (`held/`, operator release), when the
previous promoted frame had at least 50 rows and either fewer than 90 % of its keys are kept or
removals plus status moves exceed 15 % of its rows. A first run is never judged, and a reparse or a
parser restatement, which compares two frames the current code derived, passes unless the source
itself churned.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any, ClassVar

import pandas as pd
import pytest

from pipeline.connectors import dq
from pipeline.connectors.base import Connector, RawSnapshot
from pipeline.connectors.dq import (
    CHURN_MAX_EVENT_SHARE,
    CHURN_MIN_KEY_OVERLAP,
    CHURN_MIN_PREVIOUS_ROWS,
    DEFAULT_THRESHOLDS,
    run_gates,
)
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import release_held, run
from pipeline.connectors.store import Store
from tests.test_dq_gates import COLUMNS, frame


def churn(result: dq.DQResult) -> dq.Check:
    return next(c for c in result.checks if c.check == "churn")


def rekeyed(df: pd.DataFrame, prefix: str = "s:new-") -> pd.DataFrame:
    out = df.copy()
    out["record_id"] = [f"{prefix}{i}" for i in range(len(out))]
    return out


def gates(current: pd.DataFrame, previous: pd.DataFrame | None, kind: str = "proposal", **kw: Any) -> Any:
    return run_gates(current, kind, COLUMNS, [], previous=previous, fetched=current, **kw)


# ---------------------------------------------------------------------------------- the rule
def test_the_thresholds_are_the_named_first_guesses() -> None:
    assert (CHURN_MIN_PREVIOUS_ROWS, CHURN_MIN_KEY_OVERLAP, CHURN_MAX_EVENT_SHARE) == (50, 0.90, 0.15)
    assert DEFAULT_THRESHOLDS["churn_min_previous_rows"] == CHURN_MIN_PREVIOUS_ROWS
    assert DEFAULT_THRESHOLDS["churn_min_key_overlap"] == CHURN_MIN_KEY_OVERLAP
    assert DEFAULT_THRESHOLDS["churn_max_event_share"] == CHURN_MAX_EVENT_SHARE


def test_a_re_keyed_frame_is_held() -> None:
    previous = frame(200)
    result = gates(rekeyed(frame(200)), previous)
    check = churn(result)
    assert result.held and check.level == "hold"
    assert check.data["kept"] == 0 and check.data["removed"] == 200 and check.data["key_overlap"] == 0.0
    # every other gate passes: same row count, same columns, no field nulled on a common row
    assert {c.check for c in result.checks if c.level == "hold"} == {"churn"}
    assert [r for r in result.hold_reasons() if r.startswith("churn: only 0.0 % of the previous 200 keys")]


def test_a_normal_frame_passes() -> None:
    previous = frame(100)
    current = frame(100).iloc[3:].copy()  # 3 rows left the file
    current.loc[current.index[:5], "lifecycle_state"] = "contracted"  # 5 moved on
    extra = rekeyed(frame(4), prefix="s:added-")  # 4 new rows are not churn
    result = gates(pd.concat([current, extra], ignore_index=True), previous)
    check = churn(result)
    assert check.level == "pass"
    assert (check.data["removed"], check.data["status_moves"]) == (3, 5)
    assert check.data["key_overlap"] == 0.97 and check.data["event_share"] == 0.08


def test_a_first_run_is_never_held_by_it() -> None:
    result = gates(frame(500), None)
    assert churn(result).level == "info"
    assert not result.held


def test_a_small_previous_frame_is_not_judged() -> None:
    result = gates(rekeyed(frame(CHURN_MIN_PREVIOUS_ROWS - 1)), frame(CHURN_MIN_PREVIOUS_ROWS - 1))
    assert churn(result).level == "info"
    assert not result.held


@pytest.mark.parametrize(("kept", "expected"), [(90, "pass"), (89, "hold")])
def test_the_key_overlap_boundary(kept: int, expected: str) -> None:
    current = frame(100).iloc[:kept]
    check = churn(gates(current, frame(100), thresholds={"row_drift_hold_pct": 100.0}))
    assert check.level == expected, check.detail


@pytest.mark.parametrize(("moved", "expected"), [(15, "pass"), (16, "hold")])
def test_status_moves_alone_can_hold(moved: int, expected: str) -> None:
    current = frame(100)
    current.loc[: moved - 1, "lifecycle_state"] = "withdrawn"
    check = churn(gates(current, frame(100)))
    assert check.data["kept"] == 100 and check.data["status_moves"] == moved
    assert check.level == expected


def test_removals_and_status_moves_add_up() -> None:
    current = frame(100).iloc[8:].copy()  # 8 removed: overlap 92 %, above the floor
    current.loc[current.index[:8], "lifecycle_state"] = "built"  # 8 moved: 16 % together
    check = churn(gates(current, frame(100), thresholds={"row_drift_hold_pct": 100.0}))
    assert check.level == "hold" and "16.0 %" in check.detail


def test_an_opportunity_frame_compares_its_status_column() -> None:
    def notices(n: int, status: str = "open") -> pd.DataFrame:
        return pd.DataFrame(
            {
                "record_id": [f"o:{i}" for i in range(n)],
                "source_id": "o",
                "source_url": "https://example.invalid/o",
                "retrieved_at": "2026-09-12T00:00:00Z",
                "licence_id": "lic#1",
                "status": status,
                "status_raw": status,
                "status_rule": "o.map",
                "title": [f"T{i}" for i in range(n)],
                "issuer": "Agency",
                "jurisdiction": "US",
                "due_at": "2027-01-01",
                "kind": "foa",
            }
        )

    assert churn(gates(notices(100, "closed"), notices(100), kind="opportunity")).level == "hold"
    assert churn(gates(notices(100), notices(100), kind="opportunity")).level == "pass"


def test_a_source_can_override_the_thresholds() -> None:
    """`dq_thresholds` in the manifest entry (the runner passes it), like every other gate."""
    current = frame(100).iloc[:80]
    loose = {"churn_min_key_overlap": 0.5, "churn_max_event_share": 0.5, "row_drift_hold_pct": 100.0}
    assert churn(gates(current, frame(100), thresholds=loose)).level == "pass"
    assert churn(gates(current, frame(100), thresholds={"row_drift_hold_pct": 100.0})).level == "hold"


# --------------------------------------------------------------------------- through the runner
SOURCE_ID = "us.iso.ercot.gen_queue"  # open and implemented; its class is swapped for `Keyed`
T0 = dt.datetime(2026, 10, 1, 3, 7, tzinfo=dt.UTC)


class Keyed(Connector):
    """A proposal connector whose snapshot says how many rows it has and what their keys start
    with (`"<n>:<prefix>:<variant>"`); `key_suffix` stands in for a parser change that re-keys."""

    kind: ClassVar[Any] = "proposal"
    ext: ClassVar[str] = "csv"
    key_suffix: ClassVar[str] = ""

    def fetch(self) -> RawSnapshot:  # pragma: no cover - every run here injects its snapshot
        raise AssertionError("no fetch in tests")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        n, prefix, variant = raw.content.decode().split(":")
        return [{"Queue ID": f"{prefix}{i}", "Status": "Active", "Variant": variant} for i in range(int(n))]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "source_record_id": [r["Queue ID"] + self.key_suffix for r in rows],
                "lifecycle_state": "studied",
                "status_raw": "Active",
                "status_rule": "test.map",
                "capacity_mw": [float(i + 1) + (0.5 if r["Variant"] else 0.0) for i, r in enumerate(rows)],
                "name_canonical": [f"Project {r['Queue ID']}" for r in rows],
                "technology_raw": "Solar",
                "state": "TX",
            }
        )
        return self.finalize(df, rows, raw)


def _snap(content: str, when: dt.datetime) -> RawSnapshot:
    return RawSnapshot(
        content=content.encode(),
        content_type="text/csv",
        url="https://example.invalid/queue.csv",
        retrieved_at=when,
        http_status=200,
        ext="csv",
    )


@pytest.fixture()
def keyed(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> Registry:
    def connector_class(source_id: str) -> type[Connector]:
        Keyed.source_id = source_id
        return Keyed

    monkeypatch.setattr(registry, "connector_class", connector_class)
    monkeypatch.setattr(Keyed, "key_suffix", "")
    return registry


def _run(registry: Registry, store: Store, content: str, day: int, **kw: Any) -> Any:
    return run(
        SOURCE_ID, registry=registry, store=store, raw=_snap(content, T0 + dt.timedelta(days=day)), **kw
    )


def test_a_source_that_re_keys_is_held_and_can_be_released(tmp_path: pathlib.Path, keyed: Registry) -> None:
    store = Store(tmp_path)
    assert _run(keyed, store, "120:Q:", 0).status == "ok"

    held = _run(keyed, store, "120:R:", 1)
    assert held.status == "partial" and held.run["dq_status"] == "fail"
    assert [r for r in held.run["hold_reasons"] if r.startswith("churn:")], held.run["hold_reasons"]
    assert "held" in held.paths and "normalized" not in held.paths and "events" not in held.paths
    check = next(c for c in held.run["dq"]["checks"] if c["check"] == "churn")
    assert check["data"]["kept"] == 0 and check["data"]["removed"] == 120

    # An operator who has checked that the re-key is real releases it; then it is diffed as usual.
    released = release_held(
        SOURCE_ID, held.run["id"], released_by="ops@example.com", store=store, registry=keyed
    )
    assert released.run["status"] == "ok"
    assert (released.run["rows_new"], released.run["rows_gone"]) == (120, 120)


def test_a_normal_second_run_passes(tmp_path: pathlib.Path, keyed: Registry) -> None:
    store = Store(tmp_path)
    assert _run(keyed, store, "120:Q:", 0).status == "ok"
    second = _run(keyed, store, "118:Q:x", 1)  # two rows gone, every capacity nudged
    assert second.status == "ok", second.run.get("hold_reasons")
    check = next(c for c in second.run["dq"]["checks"] if c["check"] == "churn")
    assert check["level"] == "pass" and check["data"]["removed"] == 2


def test_a_first_run_of_a_big_register_is_not_held(tmp_path: pathlib.Path, keyed: Registry) -> None:
    first = _run(keyed, Store(tmp_path), "5000:Q:", 0)
    assert first.status == "ok"
    assert next(c for c in first.run["dq"]["checks"] if c["check"] == "churn")["level"] == "info"


def test_a_reparse_under_a_re_keying_parser_restates_and_passes(
    tmp_path: pathlib.Path, keyed: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make reparse` after a parser change that moves every key: the previous output is
    re-derived under the new code before the diff, so the gate sees no churn and nothing is news."""
    store = Store(tmp_path)
    first = _run(keyed, store, "120:Q:", 0)
    assert first.status == "ok"

    monkeypatch.setattr(Keyed, "key_suffix", "-v2")
    monkeypatch.setattr(Keyed, "parser_version", "9.9.9")
    again = run(SOURCE_ID, registry=keyed, store=store, reparse=True, now=T0 + dt.timedelta(days=1))
    assert again.status == "ok", again.run.get("hold_reasons")
    assert again.run["parser_restated"]["restated"] is True
    assert again.run["events_emitted"] == 0
    check = next(c for c in again.run["dq"]["checks"] if c["check"] == "churn")
    assert check["level"] == "pass" and check["data"]["kept"] == 120
    assert again.records["record_id"].str.endswith("-v2").all()

    # and the same parser change reaching the source on its next fetch is a restatement too
    nxt = _run(keyed, store, "120:Q:x", 2)
    assert nxt.status == "ok", nxt.run.get("hold_reasons")
    assert set(nxt.events["event_type"].astype(str)) <= {"capacity_change"}


def test_a_re_keying_parser_change_that_cannot_be_restated_is_held(
    tmp_path: pathlib.Path, keyed: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the stored snapshot the previous output cannot be re-derived, the diff would compare
    new keys with old ones, and every row would be published as removed + new: held instead."""
    store = Store(tmp_path)
    first = _run(keyed, store, "120:Q:", 0)
    first.paths["snapshot"].unlink()

    monkeypatch.setattr(Keyed, "key_suffix", "-v2")
    monkeypatch.setattr(Keyed, "parser_version", "9.9.9")
    second = _run(keyed, store, "120:Q:x", 1)
    assert second.run["parser_restated"]["restated"] is False
    assert second.status == "partial"
    assert [r for r in second.run["hold_reasons"] if r.startswith("churn:")]
