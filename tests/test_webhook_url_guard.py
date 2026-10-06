"""Webhook destinations are vetted at registration and pinned at delivery (architect audit
2026-09-30 A10; `services/alerts/webhook_url.py`).

No test here resolves a real name or opens a socket: resolvers are injected, and the delivery
tests run the real `httpx.Client` -> `GuardedTransport` -> `GuardedNetworkBackend` path over a
recording in-memory backend that stands in for the socket layer.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import httpcore
import pytest

from services.alerts import webhook_url
from services.alerts.webhook_url import (
    UnsafeDestination,
    address_refusal,
    guarded_client,
    validate_webhook_url,
)
from services.alerts.webhooks import attempt_delivery, create_test_delivery
from services.alerts.worker import HttpxTransport
from services.db.models import WebhookEndpoint
from services.ids import public_id
from tests.conftest import make_account, make_user

PUBLIC = "93.184.215.14"


# ------------------------------------------------------------------------------- addresses
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.10.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata (link-local)
        "100.64.0.1",  # shared address space
        "100.100.100.200",  # a metadata service inside the shared range
        "0.0.0.0",  # noqa: S104 - an address under test, not a bind
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.0.192",  # an IETF protocol-assignment address used for metadata
        "168.63.129.16",  # a cloud host service on a public address
        "198.18.0.1",
        "192.0.2.10",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd00:ec2::254",
        "fec0::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",
        "::127.0.0.1",  # IPv4-compatible
        "2002:7f00:1::1",  # 6to4 of 127.0.0.1
        "2002:a00:1::1",  # 6to4 of 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "64:ff9b:1::1",
        "2001:db8::1",
    ],
)
def test_internal_and_special_addresses_are_refused(address: str) -> None:
    assert address_refusal(address) is not None


@pytest.mark.parametrize("address", [PUBLIC, "1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert address_refusal(address) is None


# ------------------------------------------------------------------------------ registration
def _resolver(*answers: Sequence[str]) -> Any:
    """Answers each successive lookup with the next list; records every call."""
    calls: list[tuple[str, int]] = []
    queue = list(answers)

    def resolve(host: str, port: int) -> Sequence[str]:
        calls.append((host, port))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _no_lookup(host: str, port: int) -> Sequence[str]:
    raise AssertionError(f"an IP literal must not be resolved ({host})")


def test_validate_accepts_a_public_host_and_returns_its_addresses() -> None:
    vetted = validate_webhook_url("https://hooks.example.com/in", resolver=_resolver([PUBLIC]))
    assert (vetted.host, vetted.port, vetted.addresses) == ("hooks.example.com", 443, (PUBLIC,))
    assert validate_webhook_url("https://hooks.example.com:443/in", resolver=_resolver([PUBLIC])).port == 443


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/in",
        "ftp://hooks.example.com/in",
        "https://user:pw@hooks.example.com/in",
        "https://hooks.example.com:8443/in",
        "https://hooks.example.com:22/in",
        "https://127.0.0.1/in",
        "https://[::1]/in",
        "https://[::ffff:a9fe:a9fe]/in",
        "https://169.254.169.254/latest/meta-data/",
        "https:///in",
        "not a url",
        "",
    ],
)
def test_validate_refuses_bad_scheme_port_userinfo_and_internal_literals(url: str) -> None:
    with pytest.raises(UnsafeDestination):
        validate_webhook_url(url, resolver=_no_lookup)


@pytest.mark.parametrize(
    "answer",
    [["10.0.0.5"], ["127.0.0.1"], [PUBLIC, "10.0.0.5"], ["fd00::1"], ["::ffff:192.168.0.1"]],
)
def test_validate_refuses_a_name_resolving_to_any_internal_address(answer: list[str]) -> None:
    with pytest.raises(UnsafeDestination, match="resolves to"):
        validate_webhook_url("https://hooks.example.com/in", resolver=_resolver(answer))


def test_validate_refuses_a_name_that_does_not_resolve() -> None:
    def fails(host: str, port: int) -> Sequence[str]:
        raise OSError("NXDOMAIN")

    with pytest.raises(UnsafeDestination, match="does not resolve"):
        validate_webhook_url("https://nowhere.example.com/in", resolver=fails)
    with pytest.raises(UnsafeDestination, match="does not resolve"):
        validate_webhook_url("https://empty.example.com/in", resolver=lambda h, p: [])


def _api_key_headers(db: Any) -> dict[str, str]:
    from tests.conftest import make_api_key

    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, plaintext = make_api_key(db, account, user, scopes=["read:live", "write:webhooks"])
    db.commit()
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/hook",
        "https://[::1]/hook",
        "https://[::ffff:10.0.0.1]/hook",
        "https://example.com:8443/hook",
        "https://user:secret@example.com/hook",
    ],
)
def test_create_webhook_refuses_internal_destinations_and_other_ports(client: Any, db: Any, url: str) -> None:
    """Before A10's fix every one of these was stored with 201."""
    resp = client.post(
        "/v1/webhooks", json={"url": url, "types": ["event.published"]}, headers=_api_key_headers(db)
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["errors"][0]["field"] == "url"


def test_create_webhook_refuses_a_name_that_resolves_internally(
    client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook_url, "default_resolver", lambda host, port: ["10.0.0.5"])
    resp = client.post(
        "/v1/webhooks",
        json={"url": "https://internal.example.com/hook", "types": ["event.published"]},
        headers=_api_key_headers(db),
    )
    assert resp.status_code == 400, resp.text
    assert "private address" in resp.json()["errors"][0]["message"]


def test_create_webhook_accepts_a_public_destination(client: Any, db: Any) -> None:
    resp = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hook", "types": ["event.published"]},
        headers=_api_key_headers(db),
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------------- delivery
class _RecordingStream(httpcore.NetworkStream):
    def __init__(self, backend: _RecordingBackend) -> None:
        self._backend = backend
        self._reply = [backend.reply]

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._reply.pop(0) if self._reply else b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._backend.written += buffer

    def close(self) -> None:
        pass

    def start_tls(
        self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None
    ) -> Any:
        self._backend.tls_server_names.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class _RecordingBackend(httpcore.NetworkBackend):
    """Stands in for the socket layer below `GuardedNetworkBackend`: records where it was asked to
    connect and what was written, and answers with `reply`."""

    def __init__(self, reply: bytes = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n") -> None:
        self.reply = reply
        self.connects: list[tuple[str, int]] = []
        self.tls_server_names: list[str | None] = []
        self.written = b""

    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, **_: Any
    ) -> httpcore.NetworkStream:
        self.connects.append((host, port))
        return _RecordingStream(self)


def _endpoint(db: Any, url: str) -> WebhookEndpoint:
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    endpoint = WebhookEndpoint(
        public_id="",
        account_id=account.id,
        created_by_user_id=user.id,
        url=url,
        types=["event.published"],
        entity="event",
        query={},
        secret="s3cret",
    )
    db.add(endpoint)
    db.flush()
    endpoint.public_id = public_id("whe", endpoint.id)
    db.flush()
    return endpoint


NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


def _deliver(db: Any, url: str, resolver: Any, backend: _RecordingBackend) -> Any:
    delivery = create_test_delivery(db, _endpoint(db, url))
    transport = HttpxTransport(client=guarded_client(resolver=resolver, inner=backend))
    try:
        return attempt_delivery(db, delivery, transport=transport, secret="s3cret", now=NOW)
    finally:
        transport.close()


def test_delivery_connects_to_the_vetted_address_with_the_hostname_for_tls_and_host(db: Any) -> None:
    backend = _RecordingBackend()
    delivery = _deliver(db, "https://hooks.example.com/in", _resolver([PUBLIC]), backend)
    assert delivery.status == "delivered" and delivery.response_status == 204
    assert backend.connects == [(PUBLIC, 443)]
    assert backend.tls_server_names == ["hooks.example.com"]
    assert b"\r\nHost: hooks.example.com\r\n" in backend.written


def test_dns_rebinding_after_registration_is_refused_at_delivery(db: Any) -> None:
    """Registered while the name answered a public address; by delivery it answers a private one."""
    validate_webhook_url("https://rebind.example.com/in", resolver=_resolver([PUBLIC]))
    backend = _RecordingBackend()
    delivery = _deliver(db, "https://rebind.example.com/in", _resolver(["10.0.0.5"]), backend)
    assert delivery.status != "delivered"
    assert delivery.error_class == "UnsafeDestination"
    assert delivery.response_status is None
    assert backend.connects == []


def test_the_connect_uses_the_vetted_answer_not_a_second_lookup(db: Any) -> None:
    """A resolver that answers public first and private afterwards: one lookup, and the socket
    goes to the address that lookup vetted."""
    resolver = _resolver([PUBLIC], ["127.0.0.1"])
    backend = _RecordingBackend()
    delivery = _deliver(db, "https://flip.example.com/in", resolver, backend)
    assert delivery.status == "delivered"
    assert resolver.calls == [("flip.example.com", 443)]
    assert backend.connects == [(PUBLIC, 443)]


def test_redirects_are_not_followed(db: Any) -> None:
    backend = _RecordingBackend(
        b"HTTP/1.1 302 Found\r\nLocation: https://169.254.169.254/latest/\r\nContent-Length: 0\r\n\r\n"
    )
    delivery = _deliver(db, "https://hooks.example.com/in", _resolver([PUBLIC]), backend)
    assert delivery.response_status == 302 and delivery.status != "delivered"
    assert backend.connects == [(PUBLIC, 443)]


def test_proxy_variables_do_not_route_deliveries(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.1:3128")
    backend = _RecordingBackend()
    delivery = _deliver(db, "https://hooks.example.com/in", _resolver([PUBLIC]), backend)
    assert delivery.status == "delivered"
    assert backend.connects == [(PUBLIC, 443)]


@pytest.mark.parametrize(
    "url", ["https://hooks.example.com:8443/in", "https://127.0.0.1/in", "https://[fd00::1]/in"]
)
def test_a_stored_url_that_is_no_longer_allowed_is_refused_at_delivery(db: Any, url: str) -> None:
    """Rows stored before registration was checked are caught at delivery."""
    backend = _RecordingBackend()
    delivery = _deliver(db, url, _resolver([PUBLIC]), backend)
    assert delivery.error_class == "UnsafeDestination"
    assert backend.connects == []


def test_a_plain_http_url_is_refused_at_delivery(db: Any) -> None:
    backend = _RecordingBackend()
    delivery = _deliver(db, "http://hooks.example.com/in", _resolver([PUBLIC]), backend)
    assert delivery.error_class == "UnsafeDestination"
    assert backend.connects == []
