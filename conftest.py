"""Repo-root pytest configuration: import path and recorded-fixture helpers (docs/04 E-6)."""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
#: Every fixture in tests/fixtures was recorded on this date (see its README).
RECORDED_AT = dt.datetime(2026, 9, 12, 18, 0, tzinfo=dt.UTC)

from pipeline.connectors.base import RawSnapshot
from pipeline.connectors.registry import Registry


@pytest.fixture(scope="session")
def registry() -> Registry:
    return Registry()


def fixture_path(name: str) -> pathlib.Path:
    path = FIXTURES / name
    if not path.exists():
        raise FileNotFoundError(f"missing recorded fixture {path}")
    return path


def snapshot(
    name: str,
    url: str,
    content_type: str = "application/octet-stream",
    retrieved_at: dt.datetime | None = None,
) -> RawSnapshot:
    """A RawSnapshot over a recorded fixture — `fetch()` never runs in tests (docs/20 §3.1)."""
    path = fixture_path(name)
    return RawSnapshot(
        content=path.read_bytes(),
        content_type=content_type,
        url=url,
        retrieved_at=retrieved_at or RECORDED_AT,
        http_status=200,
        ext=path.suffix.lstrip("."),
    )


def connector_for(source_id: str, reg: Registry | None = None, **kwargs: object):
    return (reg or Registry()).instantiate(source_id, **kwargs)
