"""Where a customer webhook may be delivered (architect audit 2026-09-30, A10).

Before this module the only check was `url.startswith("https://")` at registration. The delivery
worker runs inside the platform's network, posts to the stored URL and stores the response status,
which `GET /v1/webhooks/{id}/deliveries` serves back. An API-tier customer could therefore point a
webhook at `https://10.0.0.5:5432/`, `https://169.254.169.254/` or a name that resolves to one of
them, and map internal hosts and ports by reading delivery status codes (server-side request
forgery).

The rule, applied twice:

- **At registration** (`POST /v1/webhooks`, `services/api/pro.py`): `validate_webhook_url` parses
  the URL, requires `https`, no user-info, port 443, resolves the host, and refuses it unless every
  address it resolves to is publicly routable (`address_refusal`).
- **At delivery** (`services/alerts/worker.py::HttpxTransport`): the same check runs inside the
  HTTP client's TCP connect (`GuardedNetworkBackend.connect_tcp`), because DNS can change after
  registration. The connect goes to the address that was just vetted, never to a second lookup, so
  a name that answers a public address to the check and a private one to the connect (DNS
  rebinding) cannot slip between them. TLS still verifies the certificate against the hostname
  (httpcore passes the URL's host as `server_hostname`), and the HTTP `Host` header is the URL's.
  The client follows no redirects and ignores proxy environment variables, so neither a `Location`
  header nor `HTTPS_PROXY` can send a delivery somewhere unvetted.

Ports: `api/openapi.yaml` (`WebhookEndpointCreate.url`, pattern `^https://`) and docs/23 §9.1 name
no port, so only the `https` default, 443, is accepted. A stored URL from before this module with
another port or scheme is refused at delivery, recorded as a failed attempt (`UnsafeDestination`).

Refused addresses (IPv4 and IPv6, with an IPv4 address embedded in an IPv6 one -- IPv4-mapped,
IPv4-compatible, 6to4, Teredo, NAT64 -- checked as the IPv4 address it reaches): loopback,
private, link-local (which holds the 169.254.169.254 cloud metadata address), unique-local,
site-local, multicast, unspecified, reserved, shared (100.64.0.0/10), documentation and benchmark
ranges, and anything else Python's `ipaddress` does not call global. Two further ranges are listed
by hand: 192.0.0.0/24 (IETF protocol assignments; Oracle Cloud's metadata service is 192.0.0.192)
and 168.63.129.16 (Azure's host service, a public address reachable only from inside Azure).

Tests never resolve a real name: every function takes a `resolver`, and `default_resolver` is the
module-level hook a test suite replaces (`tests/conftest.py`).
"""

from __future__ import annotations

import ipaddress
import socket
import typing
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import httpcore
import httpx

#: `(host, port) -> addresses`. Raises `OSError` (as `socket.getaddrinfo` does) when the name does
#: not resolve.
Resolver = Callable[[str, int], Sequence[str]]

#: docs/23 §9.1 and the spec's `^https://` pattern name no other port.
ALLOWED_PORTS: frozenset[int] = frozenset({443})

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_EXTRA_BLOCKED: tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str], ...] = (
    (ipaddress.ip_network("192.0.0.0/24"), "an IETF protocol-assignment address"),
    (ipaddress.ip_network("168.63.129.16/32"), "a cloud host-service address"),
    (ipaddress.ip_network("64:ff9b:1::/48"), "a local-use NAT64 address"),
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeDestination(Exception):
    """The URL or an address its host resolves to is not a permitted webhook destination. The
    message says why, in words safe to show the customer who registered the URL."""


def system_resolver(host: str, port: int) -> list[str]:
    """Every address `host` resolves to for a TCP connection, in resolver order, de-duplicated."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


#: The resolver used when a caller passes none. Read at call time, so a test suite can replace it.
default_resolver: Resolver = system_resolver


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    if addr.teredo is not None:
        return addr.teredo[1]
    if addr in _NAT64:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if int(addr) >> 32 == 0 and int(addr) > 1:
        # IPv4-compatible (::a.b.c.d, deprecated by RFC 4291 but still parsed by some stacks).
        return ipaddress.IPv4Address(int(addr))
    return None


def _parse_address(value: str) -> _IPAddress:
    # A scoped IPv6 literal (`fe80::1%eth0`) is link-local whatever its zone.
    return ipaddress.ip_address(value.split("%", 1)[0])


def address_refusal(value: str | _IPAddress) -> str | None:
    """Why `value` may not receive a webhook, or None when it is publicly routable."""
    addr = _parse_address(value) if isinstance(value, str) else value
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(addr)
        if embedded is not None and (reason := address_refusal(embedded)) is not None:
            return f"{reason} (embedded in {addr})"
    if addr.is_loopback:
        return "a loopback address"
    if addr.is_unspecified:
        return "an unspecified address"
    if addr.is_link_local:
        return "a link-local address"
    if addr.is_multicast:
        return "a multicast address"
    if isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local:
        return "a site-local address"
    if addr.is_private:
        return "a private address"
    if addr.is_reserved:
        return "a reserved address"
    for network, reason in _EXTRA_BLOCKED:
        if addr.version == network.version and addr in network:
            return reason
    if not addr.is_global:
        return "not a publicly routable address"
    return None


@dataclass(frozen=True)
class VettedDestination:
    host: str
    port: int
    #: Every address the host resolved to, all of them publicly routable, in resolver order.
    addresses: tuple[str, ...]


def vet_host(host: str, port: int, *, resolver: Resolver | None = None) -> VettedDestination:
    """Resolve `host` and refuse it unless the port is allowed and every address is public. A host
    that resolves to a mix of public and private addresses is refused: a client could pick either."""
    if port not in ALLOWED_PORTS:
        raise UnsafeDestination(
            f"port {port} is not allowed (only {', '.join(map(str, sorted(ALLOWED_PORTS)))})"
        )
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if not bare:
        raise UnsafeDestination("the URL has no host")
    try:
        literal: _IPAddress | None = _parse_address(bare)
    except ValueError:
        literal = None
    if literal is not None:
        addresses: Sequence[str] = [str(literal)]
    else:
        resolve = resolver if resolver is not None else default_resolver
        try:
            addresses = resolve(bare, port)
        except (OSError, UnicodeError) as exc:
            raise UnsafeDestination(f"host {bare!r} does not resolve") from exc
        if not addresses:
            raise UnsafeDestination(f"host {bare!r} does not resolve")
    for address in addresses:
        try:
            reason = address_refusal(address)
        except ValueError as exc:
            raise UnsafeDestination(f"host {bare!r} resolved to an unparseable address") from exc
        if reason is not None:
            raise UnsafeDestination(f"host {bare!r} resolves to {reason}")
    return VettedDestination(host=bare, port=port, addresses=tuple(str(a) for a in addresses))


def validate_webhook_url(url: object, *, resolver: Resolver | None = None) -> VettedDestination:
    """Registration-time check (module docstring). Raises `UnsafeDestination` with a reason."""
    if not isinstance(url, str) or not url:
        raise UnsafeDestination("url is required")
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise UnsafeDestination("url is not a valid URL") from exc
    if parsed.scheme != "https":
        raise UnsafeDestination("url must be https://")
    if parsed.userinfo:
        raise UnsafeDestination("url must not carry a user name or password")
    return vet_host(parsed.host, parsed.port or 443, resolver=resolver)


class GuardedNetworkBackend(httpcore.NetworkBackend):
    """Delivery-time check and pin (module docstring). Wraps the real socket backend: every TCP
    connect first vets the host and then connects to a vetted address, never re-resolving."""

    def __init__(
        self, *, resolver: Resolver | None = None, inner: httpcore.NetworkBackend | None = None
    ) -> None:
        self._resolver = resolver
        self._inner = inner if inner is not None else httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        vetted = vet_host(host, port, resolver=self._resolver)
        last_error: Exception | None = None
        for address in vetted.addresses:
            try:
                return self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
        # `vet_host` never returns an empty address list, so a failure was recorded.
        raise last_error if last_error is not None else UnsafeDestination(f"host {host!r} has no address")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        raise UnsafeDestination("a unix socket is not a webhook destination")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class GuardedTransport(httpx.HTTPTransport):
    """`httpx.HTTPTransport` whose connection pool connects through `GuardedNetworkBackend`.

    httpx 0.27 does not take a network backend, so the pool it builds is replaced by an identical
    one that has the guarded backend. `tests/test_webhook_url_guard.py` drives a request through
    this class end to end, so an httpx change that breaks the replacement fails a test."""

    def __init__(
        self, *, resolver: Resolver | None = None, inner: httpcore.NetworkBackend | None = None
    ) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=10,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=GuardedNetworkBackend(resolver=resolver, inner=inner),
        )


def guarded_client(
    *, timeout: float = 10.0, resolver: Resolver | None = None, inner: httpcore.NetworkBackend | None = None
) -> httpx.Client:
    """The webhook delivery client: guarded connects, no redirects, no proxy from the environment."""
    return httpx.Client(
        timeout=timeout,
        transport=GuardedTransport(resolver=resolver, inner=inner),
        follow_redirects=False,
        trust_env=False,
    )


__all__ = [
    "ALLOWED_PORTS",
    "GuardedNetworkBackend",
    "GuardedTransport",
    "Resolver",
    "UnsafeDestination",
    "VettedDestination",
    "address_refusal",
    "default_resolver",
    "guarded_client",
    "system_resolver",
    "validate_webhook_url",
    "vet_host",
]
