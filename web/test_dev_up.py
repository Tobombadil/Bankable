"""Unit tests for the optional-lane helpers in `web/dev_up.py`: the EIA-860 owner-share load and the
single `services.ingest.enrich.apply_context_features` call run inside `_load_context_asset_layers`
after every asset and edge load and before the curated parents, and each tolerates a missing module
or a missing input with one log line rather than failing the rest of `dev_up`."""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from web import dev_up


@pytest.fixture()
def no_enrich_module(monkeypatch: pytest.MonkeyPatch) -> None:
    # `None` in sys.modules makes `import services.ingest.enrich` raise ImportError deterministically,
    # whether or not another lane has landed the module by the time this runs.
    monkeypatch.setitem(sys.modules, "services.ingest.enrich", None)


def _fake_enrich(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    module = types.ModuleType("services.ingest.enrich")
    module.apply_context_features = fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.ingest.enrich", module)


def test_apply_context_features_missing_module_is_one_log_line(
    no_enrich_module: None, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._apply_context_features(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context features:")]
    assert len(lines) == 1
    assert "not available yet" in lines[0]


def test_apply_context_features_is_called_once_with_session_and_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Any, Path]] = []

    def fake(session: Any, data_root: Path) -> dict[str, int]:
        calls.append((session, data_root))
        return {"features": 1}

    _fake_enrich(monkeypatch, fake)
    session = object()
    dev_up._apply_context_features(session, tmp_path)  # type: ignore[arg-type]
    assert calls == [(session, tmp_path)]


def test_apply_context_features_missing_input_is_one_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    def fake(session: Any, data_root: Path) -> None:
        raise FileNotFoundError(data_root / "normalized" / "context" / "missing.parquet")

    _fake_enrich(monkeypatch, fake)
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._apply_context_features(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context features:")]
    assert len(lines) == 1
    assert "input not found" in lines[0]


def test_load_ownership_missing_parquet_is_one_log_line(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._load_ownership(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("asset ownership:")]
    assert len(lines) == 1
    assert "not found" in lines[0]


def test_load_ownership_calls_the_lane_loader_with_the_parquet_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import services.ingest.ownership as ownership

    parquet = tmp_path / "normalized" / "context" / "us.eia.860.owners.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"")
    calls: list[Path] = []
    monkeypatch.setattr(
        ownership, "load_owner_shares_parquet", lambda session, path: calls.append(path) or {}
    )
    dev_up._load_ownership(object(), tmp_path)  # type: ignore[arg-type]
    assert calls == [parquet]


def test_context_layers_run_owner_shares_then_features_after_the_asset_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With no midstream parquet present the file loop does nothing and `load_parents` is not
    reached, but the three post-loop steps still run, in this order: the owner-share load, the
    single features call, then the organisation graph (GLEIF parents and curated aliases,
    docs/22 §17). Ownership edges must exist before features derived from them are computed, and
    the organisation graph reads what all of them created."""
    order: list[str] = []
    monkeypatch.setattr(dev_up, "_load_ownership", lambda session, data_dir: order.append("ownership"))
    monkeypatch.setattr(
        dev_up, "_apply_context_features", lambda session, data_root: order.append("features")
    )
    monkeypatch.setattr(
        dev_up, "_load_organization_graph", lambda session, data_dir: order.append("org_graph")
    )
    (tmp_path / "normalized" / "context").mkdir(parents=True)
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._load_context_asset_layers(object(), tmp_path)  # type: ignore[arg-type]
    assert order == ["ownership", "features", "org_graph"]
    assert any("no midstream/fuels parquet" in r.getMessage() for r in caplog.records)


def test_load_organization_graph_without_a_gleif_parquet_still_applies_aliases(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Unlike the two steps above, this one always has work to do: the curated alias file ships in
    the repository, and applying it registers its source, so it needs a real session rather than
    short-circuiting on a missing file. A missing GLEIF parquet is still only a log line, and the
    aliases land regardless (docs/22 §17)."""
    from services.db.session import get_engine, get_sessionmaker, init_db

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session, caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._load_organization_graph(session, tmp_path)
    messages = [r.getMessage() for r in caplog.records]
    assert any("gleif" in m.lower() and "not found" in m.lower() for m in messages), messages
    assert any("alias" in m.lower() for m in messages), messages
