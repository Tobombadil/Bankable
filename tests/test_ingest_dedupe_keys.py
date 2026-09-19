"""Order-independent keys for duplicated source ids (audit 2026-09-18 §3.1 "Change events", item 1).

A `dedupe_strategy = "suffix"` connector used to number repeated `record_id`s by file position
(`#2`, `#3`), so a row reorder in the source swapped identities and fabricated change events.
These tests pin the replacement: the suffix is derived from the row's own disambiguating content,
so two snapshots with the same rows in a different order diff to zero events, and a previous
snapshot keyed the old positional way is re-keyed by content before the diff.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

import pandas as pd
import pytest

from pipeline.connectors.base import Connector, RawSnapshot
from pipeline.connectors.dedupe import (
    LEGACY_SUFFIX_RE,
    align_previous_keys,
    content_disambiguator,
    split_key,
)
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store

SOURCE_ID = "us.iso.nyiso.gen_queue"  # the source the audit measured this on (suffix strategy)
DAY1 = dt.datetime(2026, 9, 12, 6, 0, tzinfo=dt.UTC)
DAY2 = dt.datetime(2026, 9, 13, 6, 0, tzinfo=dt.UTC)

ROWS: list[dict[str, Any]] = [
    {
        "Queue ID": "0031",
        "Project Name": "Alpha Solar",
        "Capacity (MW)": 100.0,
        "County": "Erie",
        "Status": "Active",
    },
    {
        "Queue ID": "0031",
        "Project Name": "Beta Wind",
        "Capacity (MW)": 50.0,
        "County": "Ulster",
        "Status": "Active",
    },
    {
        "Queue ID": "0040",
        "Project Name": "Gamma BESS",
        "Capacity (MW)": 20.0,
        "County": "Kings",
        "Status": "Active",
    },
]


class RowsConnector(Connector):
    """A suffix-strategy connector whose parse() returns whatever rows the snapshot carries."""

    source_id: ClassVar[str] = SOURCE_ID
    kind: ClassVar[Any] = "proposal"
    ext: ClassVar[str] = "csv"
    dedupe_strategy: ClassVar[Any] = "suffix"

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = raw.meta["rows"]
        return [dict(r) for r in rows]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "source_record_id": [r["Queue ID"] for r in rows],
                "name_canonical": [r["Project Name"] for r in rows],
                "name_norm": [r["Project Name"].lower() for r in rows],
                "capacity_mw": [r["Capacity (MW)"] for r in rows],
                "county": [r["County"] for r in rows],
                "county_norm": [r["County"].lower() for r in rows],
                "state": ["NY"] * len(rows),
                "lifecycle_state": ["studied" if r["Status"] == "Active" else "withdrawn" for r in rows],
                "status_raw": [r["Status"] for r in rows],
                "status_rule": ["test"] * len(rows),
                "technology": ["solar_pv"] * len(rows),
                "iso": ["NYISO"] * len(rows),
                "queue_id": [r["Queue ID"] for r in rows],
            }
        )
        return self.finalize(df, rows, raw)


def snap(rows: list[dict[str, Any]], when: dt.datetime, salt: str) -> RawSnapshot:
    return RawSnapshot(
        content=(salt + repr(rows)).encode(),  # distinct bytes so the unchanged short-circuit never fires
        content_type="text/csv",
        url="https://example.invalid/queue.csv",
        retrieved_at=when,
        http_status=200,
        ext="csv",
        meta={"rows": rows},
    )


@pytest.fixture()
def connector(registry: Registry) -> RowsConnector:
    return RowsConnector(registry.get(SOURCE_ID))


@pytest.fixture()
def patched_registry(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> Registry:
    real = registry.instantiate

    def instantiate(source_id: str, **kw: Any) -> Connector:
        if source_id == SOURCE_ID:
            return RowsConnector(registry.get(source_id))
        return real(source_id, **kw)

    monkeypatch.setattr(registry, "instantiate", instantiate)
    return registry


# ---------------------------------------------------------------- finalize(): the key itself
def test_duplicate_ids_are_suffixed_by_content_not_position(connector: RowsConnector) -> None:
    s = snap(ROWS, DAY1, "a")
    forward = connector.normalize(connector.parse(s), s)
    reordered = connector.normalize(connector.parse(snap(list(reversed(ROWS)), DAY1, "b")), s)

    assert forward["record_id"].is_unique and reordered["record_id"].is_unique
    assert set(forward["record_id"]) == set(reordered["record_id"])
    # the identity follows the row's content, whatever its position in the file
    by_name = dict(zip(forward["name_canonical"], forward["record_id"], strict=True))
    by_name_reordered = dict(zip(reordered["name_canonical"], reordered["record_id"], strict=True))
    assert by_name == by_name_reordered
    # every member of a duplicated group carries the content suffix; a unique id carries none
    assert by_name["Gamma BESS"] == f"{SOURCE_ID}:0040"
    for name in ("Alpha Solar", "Beta Wind"):
        base, suffix = split_key(by_name[name])
        assert base == f"{SOURCE_ID}:0031" and suffix is not None
        assert not LEGACY_SUFFIX_RE.match(by_name[name]), "no positional #N suffix any more"
    assert int(forward.attrs["duplicates_resolved"]) == 2


def test_content_disambiguator_is_deterministic_and_ignores_status() -> None:
    a = {"name_norm": "alpha solar", "capacity_mw": 100.0, "county_norm": "erie", "state": "NY"}
    assert content_disambiguator(a) == content_disambiguator({**a, "lifecycle_state": "withdrawn"})
    assert content_disambiguator(a) != content_disambiguator({**a, "county_norm": "ulster"})
    assert content_disambiguator(a) == content_disambiguator({**a, "capacity_mw": "100.0"})


def test_byte_identical_duplicates_still_get_distinct_keys(connector: RowsConnector) -> None:
    rows = [ROWS[0], dict(ROWS[0]), ROWS[2]]
    s = snap(rows, DAY1, "twins")
    df = connector.normalize(connector.parse(s), s)
    assert df["record_id"].is_unique


# ---------------------------------------------------------------- runner: reorder -> zero events
def test_a_reordered_file_produces_no_events(tmp_path, patched_registry: Registry) -> None:
    store = Store(tmp_path)
    day1 = run(SOURCE_ID, registry=patched_registry, store=store, raw=snap(ROWS, DAY1, "a"))
    assert day1.status == "ok" and day1.run["rows_new"] == 3

    day2 = run(SOURCE_ID, registry=patched_registry, store=store, raw=snap(list(reversed(ROWS)), DAY2, "b"))
    assert day2.status == "ok"
    assert day2.run["events_emitted"] == 0, day2.events
    assert day2.run["rows_gone"] == 0 and day2.run["rows_new"] == 0
    assert day1.records is not None and day2.records is not None
    assert set(day1.records["record_id"]) == set(day2.records["record_id"])


def test_a_real_change_in_a_reordered_file_is_still_one_event(tmp_path, patched_registry: Registry) -> None:
    store = Store(tmp_path)
    run(SOURCE_ID, registry=patched_registry, store=store, raw=snap(ROWS, DAY1, "a"))
    changed = [dict(r) for r in reversed(ROWS)]
    beta = next(r for r in changed if r["Project Name"] == "Beta Wind")
    beta["Status"] = "Withdrawn"
    day2 = run(SOURCE_ID, registry=patched_registry, store=store, raw=snap(changed, DAY2, "b"))
    assert day2.events is not None
    kinds = day2.events["event_type"].astype(str).tolist()
    assert kinds == ["withdrawn"], day2.events


# ---------------------------------------------------------------- legacy positional keys on read
def test_a_previous_snapshot_with_positional_suffixes_is_rekeyed_by_content(
    tmp_path, patched_registry: Registry, connector: RowsConnector
) -> None:
    """The store already holds NYISO snapshots keyed `X`, `X#2` by file order. The first run after
    this change must not report them removed and re-added: they are matched to the new content
    keys by the same disambiguator, and only real changes produce events."""
    s1 = snap(ROWS, DAY1, "legacy")
    legacy = connector.normalize(connector.parse(s1), s1)
    # rewrite the previous snapshot the way the old finalize() keyed it: first occurrence bare,
    # the second `#2`
    base = f"{SOURCE_ID}:0031"
    legacy.loc[legacy["name_canonical"] == "Alpha Solar", "record_id"] = base
    legacy.loc[legacy["name_canonical"] == "Beta Wind", "record_id"] = base + "#2"
    aligned = align_previous_keys(legacy, connector.normalize(connector.parse(s1), s1))
    assert set(aligned["record_id"]) == set(connector.normalize(connector.parse(s1), s1)["record_id"])

    # end to end through the runner: seed the store with the legacy-keyed parquet as day 1
    store = Store(tmp_path)
    day1 = run(SOURCE_ID, registry=patched_registry, store=store, raw=s1)
    store.write_parquet(day1.paths["normalized"], legacy)  # overwrite with legacy keys
    day2 = run(SOURCE_ID, registry=patched_registry, store=store, raw=snap(list(reversed(ROWS)), DAY2, "z"))
    assert day2.run["events_emitted"] == 0, day2.events


def test_a_unique_id_that_becomes_duplicated_is_one_new_record_not_a_removal(
    tmp_path, patched_registry: Registry
) -> None:
    store = Store(tmp_path)
    filler = [  # keeps the row-count delta under the DQ gate's threshold; not what is under test
        {
            "Queue ID": f"1{i:03d}",
            "Project Name": f"Filler {i}",
            "Capacity (MW)": 5.0,
            "County": "Erie",
            "Status": "Active",
        }
        for i in range(20)
    ]
    run(SOURCE_ID, registry=patched_registry, store=store, raw=snap([ROWS[0], ROWS[2], *filler], DAY1, "a"))
    day2 = run(SOURCE_ID, registry=patched_registry, store=store, raw=snap([*ROWS, *filler], DAY2, "b"))
    assert day2.status == "ok"
    assert day2.events is not None
    assert day2.events["event_type"].astype(str).tolist() == ["new"]
    assert day2.run["rows_gone"] == 0
