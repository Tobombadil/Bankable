"""Repo-root pytest configuration: import path, recorded-fixture helpers (docs/04 E-6), and the
suite-wide test hooks every directory shares (`tests/`, `pipeline/`, `services/`, `infra/`, `web/`):

- cheap Argon2 parameters for every test except those marked `production_argon2` (audit QA-1);
- a loopback-only socket guard, opted out per test with `network` (audit QA-4, docs/04 E-13 item 4);
- the `latency` marker on every wall-clock budget test, so the parallel coverage job can leave them
  to the serial, coverage-free `perf` job (audit QA-2/QA-3; `make test-perf`).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import pathlib
import socket
import sys
from collections.abc import Iterator
from typing import Any

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


# ------------------------------------------------------------------------------- Argon2 (QA-1)
#: Argon2id at the smallest legal cost. Production keeps the library defaults (RFC 9106 low-memory
#: profile: t=3, m=64 MiB, p=4), which cost ~0.18 s per hash or verify here; with 1,400+ core and
#: 280 web tests that create users or log in, that was 15-50 % of the auth-heavy files' wall time.
#: A hash carries its own parameters, so a cheap hash still verifies under the production hasher.
_CHEAP_ARGON2: dict[str, int] = {"time_cost": 1, "memory_cost": 8, "parallelism": 1}


@pytest.fixture(autouse=True)
def _cheap_password_hashing(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap `services.api.auth._hasher` for a cheap one, unless the test is marked
    `production_argon2` (`tests/test_password_hash_parameters.py` pins the production parameters).
    Only patches when the module is already imported: collection imports it for every test that
    can hash a password, and a connector test should not pay for importing the API."""
    if request.node.get_closest_marker("production_argon2"):
        return
    auth = sys.modules.get("services.api.auth")
    if auth is None:
        return
    from argon2 import PasswordHasher

    monkeypatch.setattr(auth, "_hasher", PasswordHasher(**_CHEAP_ARGON2))


# ------------------------------------------------------------------------ socket guard (QA-4)
#: Proxy variables are dropped for the run so a fetch cannot leave through a loopback proxy (this
#: sandbox's egress proxy listens on 127.0.0.1) and look like a local connection.
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_ALLOW_NETWORK_ENV = "PYTEST_ALLOW_NETWORK"
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_network_allowed = False


class NetworkBlocked(RuntimeError):
    """A test opened a non-loopback connection without the `network` marker. Not an `OSError`, so
    retry loops and `except OSError` fallbacks cannot swallow it and pass offline by accident."""


def _is_local(address: Any) -> bool:
    if not isinstance(address, tuple):  # AF_UNIX path (or abstract socket name)
        return True
    host = str(address[0])
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # a hostname other than localhost: resolution would reach the network


def _guarded(real: Any) -> Any:
    def connect(self: socket.socket, address: Any) -> Any:
        inet = self.family in (socket.AF_INET, socket.AF_INET6)
        if inet and not _network_allowed and not _is_local(address):
            raise NetworkBlocked(
                f"test tried to connect to {address!r}; tests are offline (docs/04 E-13 item 4). "
                "Serve a recorded fixture, or mark the test `network` if it must reach a real host."
            )
        return real(self, address)

    return connect


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get(_ALLOW_NETWORK_ENV) == "1":
        return
    config._bankable_saved_proxies = {k: os.environ.pop(k) for k in _PROXY_VARS if k in os.environ}  # type: ignore[attr-defined]
    socket.socket.connect = _guarded(_real_connect)  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded(_real_connect_ex)  # type: ignore[method-assign]


def pytest_unconfigure(config: pytest.Config) -> None:
    socket.socket.connect = _real_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _real_connect_ex  # type: ignore[method-assign]
    os.environ.update(getattr(config, "_bankable_saved_proxies", {}))


@pytest.fixture(autouse=True)
def _network_marker(request: pytest.FixtureRequest) -> Iterator[None]:
    global _network_allowed
    if request.node.get_closest_marker("network") is None:
        yield
        return
    saved = dict(getattr(request.config, "_bankable_saved_proxies", {}))
    previous = {k: os.environ.get(k) for k in saved}
    os.environ.update(saved)
    _network_allowed = True
    try:
        yield
    finally:
        _network_allowed = False
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ------------------------------------------------------------------- latency budgets (QA-2/QA-3)
#: Wall-clock budget tests, by node id prefix. They run serially and without coverage in CI's
#: `perf` job (`make test-perf`); the parallel, coverage-instrumented `test-core` job deselects
#: them (`-m "not latency"`), because their timings mean nothing on a loaded, traced process.
#: Marked here rather than in each module so the list of timing tests is one reviewable place.
LATENCY_TESTS = (
    "tests/test_api_geo_performance.py",
    "tests/test_api_geo_performance_full_store.py",
    "tests/test_api_assets_lines.py::test_national_and_regional_views_over_3000_synthetic_lines_are_small_and_warm_fast",
)


#: Browser end-to-end modules: each starts a uvicorn subprocess on a fixed port and drives Chromium.
#: Under `pytest -n N --dist loadgroup` they share one worker, so two browsers and two app servers
#: never compete for the same CPUs (or ports: the PMTiles proof server and the alerts e2e server
#: both default to 8798).
BROWSER_TESTS = ("web/test_e2e.py", "web/test_e2e_alerts.py")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Marks the latency tests, and under `--dist loadgroup` (`make test-core`, `make test-web`)
    groups every test by its file, as `--dist loadfile` would (76 module-scoped fixtures assume
    one worker per module), except that the browser modules form one group. Runs first so the
    `-m` deselection and xdist's own grouping see these markers."""
    # A worker sees `dist == "no"` and `loadgroup = True` (xdist/remote.py `setup_config`).
    group_by_file = config.pluginmanager.hasplugin("xdist") and (
        config.getoption("dist", "no") == "loadgroup" or bool(getattr(config.option, "loadgroup", False))
    )
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        if item.nodeid.startswith(LATENCY_TESTS):
            item.add_marker(pytest.mark.latency)
        if group_by_file:
            item.add_marker(pytest.mark.xdist_group("browser" if path in BROWSER_TESTS else path))
