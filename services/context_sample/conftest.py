from __future__ import annotations

import pathlib

import pytest

from services.context_sample.fixture_build import build_fixture_layers


@pytest.fixture(scope="session")
def fixture_prospect_dir(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Every prospect layer built once from the recorded fixtures (offline)."""
    out = tmp_path_factory.mktemp("prospect")
    report = build_fixture_layers(out)
    assert not report["failures"], report["failures"]  # noqa: S101 - fixture precondition
    return out
