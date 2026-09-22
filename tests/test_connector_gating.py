"""Publication gating is enforced in code, not convention (docs/21 §8, docs/04 DA-11, CLAUDE.md).

The rules under test:
1. A source whose registry `reuse` is `restricted` or `unknown` refuses to run without an explicit
   `allow_restricted=True`.
2. Even with the flag, its outputs go to the quarantine store, which cannot write a publishable path.
3. Private aggregators have no connector at all and fail to register under any flag.
"""

from __future__ import annotations

import pytest

from pipeline.connectors.base import GateViolation
from pipeline.connectors.registry import GATED_REUSE, RegistrationError, Registry
from pipeline.connectors.store import QuarantineStore, Store

GATED_SOURCES = [
    "us.iso.spp.gen_queue",
    "us.iso.isone.gen_queue",
    "us.iso.pjm.gen_queue",
    "us.iso.miso.gen_queue",
]
PUBLISHABLE_SOURCES = ["us.iso.ercot.gen_queue", "us.eia.860m", "us.grants_gov.search2"]


@pytest.mark.parametrize("source_id", GATED_SOURCES)
def test_a_gated_source_refuses_to_run(registry: Registry, source_id: str) -> None:
    assert registry.get(source_id).reuse in GATED_REUSE
    with pytest.raises(GateViolation) as e:
        registry.instantiate(source_id)
    assert source_id in str(e.value)
    assert "allow_restricted" in str(e.value)


@pytest.mark.parametrize("source_id", GATED_SOURCES)
def test_a_gated_source_has_no_connector_this_sprint(registry: Registry, source_id: str) -> None:
    """SPP, ISO-NE, PJM and MISO are explicitly out of scope for Sprint 1."""
    assert not registry.get(source_id).implemented
    with pytest.raises(RegistrationError):
        registry.connector_class(source_id)


def test_the_flag_is_the_only_way_past_the_gate(registry: Registry) -> None:
    with pytest.raises(RegistrationError):  # gate passed, connector still absent
        registry.instantiate("us.iso.spp.gen_queue", allow_restricted=True)


@pytest.mark.parametrize("source_id", PUBLISHABLE_SOURCES)
def test_open_and_attribution_sources_run_without_a_flag(registry: Registry, source_id: str) -> None:
    connector = registry.instantiate(source_id)
    assert connector.source_id == source_id
    assert registry.get(source_id).reuse not in GATED_REUSE


def test_private_aggregators_fail_to_register(registry: Registry) -> None:
    entry = registry.get("us.gridtracker.interconnection_fyi")
    assert entry.never_ingest
    with pytest.raises(RegistrationError):
        registry.connector_class("us.gridtracker.interconnection_fyi")


def test_carbonstorage_is_excluded_by_id_not_by_prose(registry: Registry) -> None:
    """The id must carry the exclusion, so editing the note cannot lift it.

    `SourceEntry.never_ingest` also substring-matches `NEVER_INGEST_NAMES` against
    `name + url + notes`. `global.carbonstorage_io` first read as excluded only because its own
    note mentions Cleanview as a comparison -- prose, one tidy-up away from silently removing a
    CLAUDE.md guardrail. This asserts the id-based rule holds on its own by rebuilding the entry
    with every prohibited name scrubbed from its text.
    """
    from dataclasses import replace

    entry = registry.get("global.carbonstorage_io")
    assert entry.never_ingest
    scrubbed = replace(entry, name="A tracker", url="https://example.invalid/", notes="")
    assert scrubbed.never_ingest, "exclusion depends on note text, not on the id"
    with pytest.raises(RegistrationError):
        registry.connector_class("global.carbonstorage_io")


def test_registry_status_labels_every_source(registry: Registry) -> None:
    states = {row["id"]: row["state"] for row in registry.status()}
    assert states["us.iso.ercot.gen_queue"] == "implemented"
    assert states["us.iso.pjm.gen_queue"] == "gated"
    assert states["us.gridtracker.interconnection_fyi"] == "excluded"
    assert states["us.eia.api"] == "unimplemented"
    assert set(states.values()) <= {"implemented", "gated", "excluded", "unimplemented"}


def test_quarantine_store_cannot_write_a_publishable_path(tmp_path) -> None:
    import pandas as pd

    q = QuarantineStore(tmp_path)
    assert q.publishable is False
    assert "quarantine" in q.normalized_path("x", "t").parts
    with pytest.raises(RuntimeError):  # a path outside the quarantine tree
        q.write_parquet(tmp_path / "normalized" / "x" / "t.parquet", pd.DataFrame({"a": [1]}))
    q.write_parquet(q.normalized_path("x", "t"), pd.DataFrame({"a": [1]}))  # inside it is fine
    assert not (tmp_path / "normalized" / "x" / "t.parquet").exists()
    # the plain store may
    s = Store(tmp_path)
    s.write_parquet(s.normalized_path("x", "t"), pd.DataFrame({"a": [1]}))
    assert s.normalized_path("x", "t").exists()


def test_gated_runs_are_routed_to_quarantine(monkeypatch, tmp_path, registry: Registry) -> None:
    """A gated source that *did* have a connector would still write nothing publishable."""
    import datetime as dt

    import pandas as pd

    from pipeline.connectors.base import Connector, RawSnapshot
    from pipeline.connectors.runner import run

    source = registry.get("us.iso.spp.gen_queue")

    class Fake(Connector):
        source_id = source.id
        kind = "proposal"
        ext = "csv"

        def parse(self, raw: RawSnapshot) -> list[dict[str, object]]:
            return [{"Queue ID": "GEN-2026-001", "Status": "Active"}]

        def normalize(self, rows, raw):
            df = pd.DataFrame(
                {
                    "source_record_id": ["GEN-2026-001"],
                    "lifecycle_state": ["studied"],
                    "status_raw": ["Active"],
                    "status_rule": ["spp.map"],
                    "capacity_mw": [1.0],
                    "name_canonical": ["x"],
                    "technology_raw": ["Solar"],
                    "state": ["KS"],
                }
            )
            return self.finalize(df, rows, raw)

    monkeypatch.setattr(registry, "connector_class", lambda sid: Fake)
    raw = RawSnapshot(
        content=b"queue",
        content_type="text/csv",
        url="https://example.invalid/spp.csv",
        retrieved_at=dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
        http_status=200,
        ext="csv",
    )
    with pytest.raises(GateViolation):
        run(source.id, registry=registry, store=Store(tmp_path), raw=raw)
    result = run(source.id, registry=registry, store=Store(tmp_path), allow_restricted=True, raw=raw)
    assert result.status == "ok"
    assert result.run["publishable"] is False
    for path in result.paths.values():
        assert "quarantine" in str(path)
    assert not (tmp_path / "normalized").exists()
    assert not (tmp_path / "events").exists()
