# ruff: noqa: S101 - asserts are the point (docs/04 E-6)
"""pipeline.context.prospect.build.run: one source failing never stops the others or the manifest."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store
from pipeline.context.prospect import build as build_mod

FIX = pathlib.Path(__file__).parent / "fixtures"


def _rogue(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    (out / "rogue.parquet").write_bytes(b"not a layer")
    return [{"layer_id": "not_in_the_catalogue", "file": "rogue.parquet", "provenance": {"source_id": "x"}}]


def _missing_file(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    return [{"layer_id": "water_stress", "file": "never_written.parquet", "provenance": {"source_id": "x"}}]


def test_a_source_that_builds_an_unknown_layer_fails_alone(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalogue check and the file hashes run inside the per-source try: before, an unknown layer id
    raised out of `run` and no manifest was written for the sources that did build."""
    monkeypatch.setitem(build_mod.SOURCES, "rogue", (_rogue, "rogue", "nothing"))
    monkeypatch.setitem(build_mod.SOURCES, "half", (_missing_file, "half", "nothing"))
    report = build_mod.run(
        region="texas", sources=["rogue", "half", "reserves"], out_dir=tmp_path, snapshots=FIX, store=None
    )
    assert "catalog.py does not list" in report["failures"]["rogue"]
    assert "FileNotFoundError" in report["failures"]["half"]
    assert report["built"] == ["oil_gas_reserves_state"]
    manifest = json.loads((tmp_path / "layers.json").read_text())
    assert sorted(manifest["layers"]) == ["oil_gas_reserves_state"]
    assert set(manifest["failures"]) == {"rogue", "half"}


def test_a_source_row_that_disappeared_does_not_stop_the_manifest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A layer whose source row is gone from data/sources.yaml stays in the manifest (the sampler withholds
    it: no row, no publication) and the run reports it; the manifest is still written."""
    first = build_mod.run(region="texas", sources=["reserves"], out_dir=tmp_path, snapshots=FIX, store=None)
    assert not first["failures"]
    real = Registry()
    trimmed = tmp_path / "sources.yaml"
    import yaml

    doc = yaml.safe_load(real.path.read_text(encoding="utf-8"))
    rows = doc["sources"] if isinstance(doc, dict) else doc
    kept = [r for r in rows if r.get("id") != "us.eia.proved_reserves"]
    trimmed.write_text(yaml.safe_dump({"sources": kept} if isinstance(doc, dict) else kept), encoding="utf-8")
    monkeypatch.setattr(build_mod, "Registry", lambda: Registry(trimmed))
    report = build_mod.run(region="texas", sources=["seismic"], out_dir=tmp_path, snapshots=FIX, store=None)
    assert "no row in data/sources.yaml" in report["failures"]["registry:us.eia.proved_reserves"]
    manifest = json.loads((tmp_path / "layers.json").read_text())
    assert {"oil_gas_reserves_state", "seismic_pga_2pct50"} <= set(manifest["layers"])
    assert "us.eia.proved_reserves" not in manifest["sources"] and "us.usgs.nshm2023" in manifest["sources"]
