"""Framework-level tests: the base contract, the registry over data/sources.yaml, and the store.

These are the rules every connector inherits (docs/20 §3.1, docs/21 §3–§4, docs/04 DA-2, DA-12).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from pipeline.connectors.base import (
    OPPORTUNITY_COLUMNS,
    PROPOSAL_COLUMNS,
    PROVENANCE,
    Connector,
    ParseError,
    RawSnapshot,
    content_hash,
    raw_json,
)
from pipeline.connectors.registry import Registry

IMPLEMENTED = [
    "us.iso.ercot.gen_queue",
    "us.iso.ercot.large_load_queue",
    "us.iso.caiso.gen_queue",
    "us.iso.nyiso.gen_queue",
    "us.eia.860m",
    "us.grants_gov.search2",
    "eu.ted.api",
    "gb.find_a_tender",
    "gb.neso.tec_register",
    "mdb.worldbank.procnotices",
]


# ---------------------------------------------------------------- RawSnapshot
def test_raw_snapshot_requires_utc():
    with pytest.raises(ValueError, match="timezone-aware"):
        RawSnapshot(
            content=b"x",
            content_type="text/csv",
            url="https://example.invalid/x",
            retrieved_at=dt.datetime(2026, 9, 12),  # noqa: DTZ001 - the point of the test
            http_status=200,
            ext="csv",
        )


def test_raw_snapshot_hashes_and_formats_its_timestamp():
    snap = RawSnapshot(
        content=b"abc",
        content_type="text/csv",
        url="https://example.invalid/x",
        retrieved_at=dt.datetime(2026, 9, 12, 5, 30, tzinfo=dt.UTC),
        http_status=200,
        ext="csv",
    )
    assert snap.sha256 == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    assert snap.retrieved_at_iso == "2026-09-12T05:30:00Z"


# ---------------------------------------------------------------- identity helpers
def test_content_hash_is_stable_and_order_sensitive():
    assert content_hash("a", "b") == content_hash("a", "b")
    assert content_hash("a", "b") != content_hash("b", "a")
    assert content_hash(None, "b") == content_hash("", "b")
    assert content_hash("a").startswith("h")


def test_raw_json_nulls_nan_and_serialises_dates():
    payload = raw_json({"a": float("nan"), "b": pd.NaT, "c": pd.Timestamp("2026-09-12"), "d": "x"})
    assert '"a": null' in payload and '"b": null' in payload
    assert '"2026-09-12T00:00:00"' in payload and '"d": "x"' in payload


# ---------------------------------------------------------------- finalize contract
class _Tiny(Connector):
    source_id = "us.eia.860m"
    kind = "proposal"
    ext = "csv"

    def parse(self, raw: RawSnapshot) -> list[dict[str, object]]:
        return [{"Plant ID": 1, "Generator ID": "A"}]

    def normalize(self, rows, raw):
        return self.finalize(pd.DataFrame({"source_record_id": ["1-A"]}), rows, raw)


def _snap() -> RawSnapshot:
    return RawSnapshot(
        content=b"x",
        content_type="text/csv",
        url="https://example.invalid/x",
        retrieved_at=dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
        http_status=200,
        ext="csv",
    )


def test_finalize_stamps_provenance_and_orders_columns(registry):
    c = _Tiny(registry.get("us.eia.860m"))
    raw = _snap()
    df = c.normalize(c.parse(raw), raw)
    assert list(df.columns) == PROPOSAL_COLUMNS
    for col in PROVENANCE:
        assert df[col].notna().all()
    assert df["record_id"].iloc[0] == "us.eia.860m:1-A"
    assert df["raw"].iloc[0] == '{"Generator ID": "A", "Plant ID": 1}'


def test_finalize_refuses_rows_without_a_source_record_id(registry):
    class _NoId(_Tiny):
        def normalize(self, rows, raw):
            return self.finalize(pd.DataFrame({"source_record_id": [None]}), rows, raw)

    c = _NoId(registry.get("us.eia.860m"))
    raw = _snap()
    with pytest.raises(ParseError, match="source_record_id"):
        c.normalize(c.parse(raw), raw)


def test_finalize_refuses_misaligned_rows_and_records(registry):
    class _Mismatch(_Tiny):
        def normalize(self, rows, raw):
            return self.finalize(pd.DataFrame({"source_record_id": ["1-A", "2-B"]}), rows, raw)

    c = _Mismatch(registry.get("us.eia.860m"))
    raw = _snap()
    with pytest.raises(ParseError):
        c.normalize(c.parse(raw), raw)


def test_a_connector_refuses_a_source_it_is_not_bound_to(registry):
    with pytest.raises(ValueError, match="bound to"):
        _Tiny(registry.get("us.iso.caiso.gen_queue"))


# ---------------------------------------------------------------- registry
def test_every_sprint1_connector_registers(registry: Registry):
    for source_id in IMPLEMENTED:
        connector = registry.instantiate(source_id)
        assert connector.source_id == source_id
        assert connector.kind in ("proposal", "opportunity")
        assert connector.egress in ("plain", "browser", "residential", "api_key")
        import importlib

        module = importlib.import_module(connector.__class__.__module__)
        doc = module.__doc__ or ""
        # docs/20 §3.1: the source_record_id strategy is recorded in the connector docstring
        assert source_id in doc, f"{source_id} module docstring must name the source id"
        assert "source_record_id" in doc or connector.kind == "opportunity", source_id


def test_opportunity_connectors_emit_opportunity_fields_not_proposal_fields(registry: Registry):
    for source_id in ("us.grants_gov.search2", "eu.ted.api", "gb.find_a_tender", "mdb.worldbank.procnotices"):
        connector = registry.instantiate(source_id)
        assert connector.kind == "opportunity"
        assert connector.columns == OPPORTUNITY_COLUMNS
        for field in (
            "kind",
            "issuer",
            "title",
            "jurisdiction",
            "technologies",
            "open_at",
            "due_at",
            "status",
        ):
            assert field in connector.columns
        assert "capacity_mw" not in connector.columns and "lifecycle_state" not in connector.columns


def test_proposal_connectors_emit_proposal_fields(registry: Registry):
    for source_id in (
        "us.iso.ercot.gen_queue",
        "us.iso.caiso.gen_queue",
        "us.iso.nyiso.gen_queue",
        "us.eia.860m",
        "gb.neso.tec_register",
        "us.iso.ercot.large_load_queue",
    ):
        connector = registry.instantiate(source_id)
        assert connector.kind == "proposal"
        assert connector.columns == PROPOSAL_COLUMNS
        assert "lifecycle_state" in connector.columns


def test_licence_id_is_derived_from_the_recorded_terms(registry: Registry):
    entry = registry.get("us.iso.ercot.gen_queue")
    assert entry.licence_id.startswith("us.iso.ercot.gen_queue#")
    assert entry.licence_id == registry.get("us.iso.ercot.gen_queue").licence_id
    other = registry.get("us.iso.caiso.gen_queue")
    assert other.licence_id != entry.licence_id


def test_unknown_sources_raise(registry: Registry):
    from pipeline.connectors.registry import RegistrationError

    with pytest.raises(RegistrationError):
        registry.get("no.such.source")


def test_the_registry_reads_every_source_in_the_manifest(registry: Registry):
    assert len(registry.ids()) >= 60
    assert registry.version
    for entry in registry.sources.values():
        assert entry.id and entry.category and entry.reuse
        assert entry.max_rps > 0


# ---------------------------------------------------------------- store
def test_store_paths_follow_the_documented_layout(tmp_path):
    from pipeline.connectors.store import Store

    s = Store(tmp_path)
    assert (
        s.snapshot_path("a.b", "20260912T060000Z", "xlsx")
        == tmp_path / "snapshots" / "a.b" / "20260912T060000Z.xlsx"
    )
    assert s.normalized_path("a.b", "t").parent == tmp_path / "normalized" / "a.b"
    assert s.events_path("a.b", "t").parent == tmp_path / "events" / "a.b"
    assert s.run_path("a.b", "t").suffix == ".json"


def test_dq_history_only_counts_successful_runs(tmp_path):
    from pipeline.connectors.store import Store

    s = Store(tmp_path)
    s.write_run("a.b", "1", {"status": "ok", "stats": {"rows": 10}})
    s.write_run("a.b", "2", {"status": "failed", "stats": {"rows": 1}})
    s.write_run("a.b", "3", {"status": "unchanged", "stats": {"rows": 10}})
    assert s.dq_history("a.b") == [{"rows": 10}, {"rows": 10}]
