"""Who is calling: the one place the API decides a request's client address (devops audit
2026-09-30 F1; architect audit A3).

Production puts three hops in front of this process: Cloudflare, Caddy and, for every page view,
the `web` service's own server-side call. Every per-address rate limit used to key on
`request.client.host`, which is Caddy's or web's container address, so all visitors shared one
bucket. The address is now established hop by hop, and each hop trusts only the one before it:

1. **Caddy** (`infra/compose/Caddyfile`) trusts `CF-Connecting-IP` only from Cloudflare's published
   ranges, and overwrites `X-Forwarded-For` with that one address. A request that reaches the
   origin directly gets its real peer address, and whatever `X-Forwarded-For` it sent is dropped.
2. **uvicorn** (`infra/entrypoint.py`) believes `X-Forwarded-For` only from Caddy's fixed address
   (`FORWARDED_ALLOW_IPS`), so `request.client.host` is the visitor for anything Caddy proxied.
3. **web** calls this API server-side and forwards its visitor's address in `X-Visitor-IP`
   (`web/api_client.py`). This module believes that header **only** alongside a matching
   `X-Internal-Token`, the service identity `web` already presents. Without the token the header
   is ignored, so it cannot be spoofed from the internet (Caddy also strips both headers from
   inbound requests).

`rate_limit_address` is what the limiters key on: the full address for IPv4, and the /64 network
for IPv6, because one IPv6 subscriber normally holds a whole /64 and could otherwise rotate
through it to get a fresh bucket per request.
"""

from __future__ import annotations

import hmac
import ipaddress
import os

from fastapi import Request

INTERNAL_TOKEN_HEADER = "x-internal-token"  # noqa: S105 -- a header name, not a secret
VISITOR_IP_HEADER = "x-visitor-ip"
UNKNOWN = "unknown"


def parse_ip(value: str | None) -> str | None:
    """The canonical text of an IP address, or `None` when `value` is not exactly one address."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def is_internal_request(request: Request) -> bool:
    """The request carries the site's service identity: a non-empty `API_INTERNAL_TOKEN` that
    matches `X-Internal-Token`, compared in constant time. Read per request, like before."""
    token = os.environ.get("API_INTERNAL_TOKEN")
    if not token:
        return False
    return hmac.compare_digest(request.headers.get(INTERNAL_TOKEN_HEADER, ""), token)


def client_ip(request: Request) -> str:
    """The visitor's address. `X-Visitor-IP` counts only on an internal request and only when it
    holds a valid address; otherwise the connection's peer as uvicorn resolved it."""
    if is_internal_request(request):
        forwarded = parse_ip(request.headers.get(VISITOR_IP_HEADER))
        if forwarded is not None:
            return forwarded
    host = request.client.host if request.client else None
    return parse_ip(host) or host or UNKNOWN


def rate_limit_address(request: Request) -> str:
    """The per-address limiter key: the IPv4 address, or the IPv6 /64 it sits in."""
    address = client_ip(request)
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return address


def client_ip_prefix(request: Request) -> str | None:
    """The coarse address stored with a session or key and used by the auth limiters: IPv4 /24
    (docs/21 §3.17 `last_used_ip`) or IPv6 /64. `None` when the request has no client at all."""
    if request.client is None and not is_internal_request(request):
        return None
    address = client_ip(request)
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    prefix = 24 if parsed.version == 4 else 64
    network = ipaddress.ip_network(f"{parsed}/{prefix}", strict=False)
    return str(network.network_address) if parsed.version == 4 else str(network)
