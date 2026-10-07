"""The suite-wide hooks in the root `conftest.py` do what they claim, and production keeps its
password-hashing cost (audit 2026-10-07 QA-1, QA-2, QA-4)."""

from __future__ import annotations

import socket
import urllib.request

import pytest
from argon2 import PasswordHasher
from argon2.profiles import RFC_9106_LOW_MEMORY

import conftest as root_conftest
from services.api import auth


# ----------------------------------------------------------------------------------- Argon2
@pytest.mark.production_argon2
def test_production_password_hashing_is_at_least_the_rfc_9106_low_memory_profile() -> None:
    """The cheap test hasher must never leak into production: `services.api.auth` keeps at least
    argon2-cffi's default cost (RFC 9106 low-memory profile: t=3, m=64 MiB, p=4)."""
    hasher = auth._hasher
    assert hasher.type is RFC_9106_LOW_MEMORY.type
    assert hasher.time_cost >= RFC_9106_LOW_MEMORY.time_cost
    assert hasher.memory_cost >= RFC_9106_LOW_MEMORY.memory_cost
    assert hasher.parallelism >= RFC_9106_LOW_MEMORY.parallelism
    hashed = auth.hash_password("correct horse battery staple")
    assert f"m={hasher.memory_cost},t={hasher.time_cost},p={hasher.parallelism}" in hashed
    assert auth.verify_password(hashed, "correct horse battery staple") is True


def test_tests_hash_with_the_cheap_parameters_and_production_still_verifies_them() -> None:
    assert auth._hasher.memory_cost == root_conftest._CHEAP_ARGON2["memory_cost"]
    hashed = auth.hash_password("pw-1234567")
    assert "$m=8,t=1,p=1$" in hashed
    # A hash carries its own parameters: a user created in a test logs in under production cost.
    assert PasswordHasher().verify(hashed, "pw-1234567") is True


# ----------------------------------------------------------------------------- socket guard
def test_a_connection_to_a_real_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolution is faked so the test needs no DNS: the guard sits on connect, after resolution.
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: public)
    with pytest.raises(root_conftest.NetworkBlocked, match="tests are offline"):
        urllib.request.urlopen("https://example.org/", timeout=5)


def test_a_connection_to_a_public_address_is_refused_without_dns() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(root_conftest.NetworkBlocked):
            sock.connect(("93.184.215.14", 443))
        with pytest.raises(root_conftest.NetworkBlocked):
            sock.connect_ex(("93.184.215.14", 443))


def test_loopback_connections_are_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=5):
            pass


@pytest.mark.parametrize(
    ("address", "local"),
    [
        (("127.0.0.1", 80), True),
        (("127.8.9.10", 80), True),
        (("::1", 80, 0, 0), True),
        (("localhost", 80), True),
        ("/tmp/some.sock", True),  # noqa: S108 -- an AF_UNIX path, never opened
        (("10.0.0.1", 80), False),
        (("169.254.169.254", 80), False),
        (("example.org", 443), False),
    ],
)
def test_only_loopback_counts_as_local(address: object, local: bool) -> None:
    assert root_conftest._is_local(address) is local


# ------------------------------------------------------------------------------ latency set
def test_the_latency_list_names_files_and_tests_that_exist() -> None:
    """A renamed timing test must not silently drop out of the `perf` job and back into the
    parallel, coverage-instrumented run."""
    for node in root_conftest.LATENCY_TESTS:
        path, _, name = node.partition("::")
        source = (root_conftest.ROOT / path).read_text(encoding="utf-8")
        assert not name or f"def {name}(" in source, node
