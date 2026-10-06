"""infra/compose/Caddyfile: routing, host names, the www redirect and the client address
(devops audit 2026-09-30 F1/F2, architect A3/A7, PM-3).

Two layers, like infra/test_compose.py:

- **Static checks, always run.** A small Caddyfile tokenizer (comments, braces, quotes; enough for
  this file) reads the API's path prefixes, the per-environment host sets and the trusted proxy
  list, and checks them against the API's own route table, `infra/terraform/dns.tf` and the
  vendored Cloudflare ranges. No binary needed.
- **Caddy itself, when a binary is available** (`CADDY_BIN`, else `caddy` on PATH; CI installs
  2.8.4, the line `compose.prod.yml` pins). The real Caddyfile is run with only its two upstream
  addresses pointed at stub servers and a local CA in place of Let's Encrypt, and real HTTPS
  requests prove every route, the redirects, which address reaches the upstream in
  `X-Forwarded-For`, and that internal headers are stripped. Skipped, not faked, without it.

Checks use `expect` instead of bare `assert` (ruff S101, see infra/scheduler/test_jobs.py).
"""

from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "infra" / "compose" / "Caddyfile"
DNS_TF = ROOT / "infra" / "terraform" / "dns.tf"
CADDY = os.environ.get("CADDY_BIN") or shutil.which("caddy")
DOMAIN = "example.test"

#: The API owns exactly these prefixes; everything else is the web app (admin UI included).
API_PATHS = ("/v1/*", "/admin/v1/*", "/feeds/*", "/webhooks/*")

#: https://www.cloudflare.com/ips-v4 and /ips-v6, retrieved 2026-09-30 (etag
#: 38f79d050aa027e3be3865e495dcc9bc on https://api.cloudflare.com/client/v4/ips). The Caddyfile
#: carries the same list; update both together.
CLOUDFLARE_RANGES = (
    "173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 "
    "190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 "
    "104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 "
    "2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"
).split()

HOSTS = {
    "production": {
        DOMAIN: "site",
        f"admin.{DOMAIN}": "admin",
        f"api.{DOMAIN}": "api",
        f"www.{DOMAIN}": "www",
    },
    "staging": {
        f"staging.{DOMAIN}": "site",
        f"admin-staging.{DOMAIN}": "admin",
        f"api-staging.{DOMAIN}": "api",
    },
}


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


# ------------------------------------------------------------------------------ a small parser
Block = tuple[list[str], list[Any]]  # (the tokens on the line that opened it, its children)


def _lines(text: str) -> list[list[str]]:
    """Caddyfile lines as token lists, with comments removed and braces as their own tokens."""
    out = []
    for raw in text.splitlines():
        tokens: list[str] = []
        for token in re.findall(r'"[^"]*"|\S+', raw):
            if token.startswith("#"):
                break
            tokens.append(token.strip('"'))
        if tokens:
            out.append(tokens)
    return out


def parse(text: str) -> list[Block]:
    """A tree of blocks. Lines without a brace become leaf blocks with no children."""
    root: list[Block] = []
    stack = [root]
    for tokens in _lines(text):
        if tokens == ["}"]:
            stack.pop()
            continue
        if tokens[-1] == "{":
            block: Block = (tokens[:-1], [])
            stack[-1].append(block)
            stack.append(block[1])
        else:
            stack[-1].append((tokens, []))
    expect(len(stack) == 1, "unbalanced braces in the Caddyfile")
    return root


def find(tree: list[Block], *head: str) -> Block:
    for block in tree:
        if tuple(block[0][: len(head)]) == head:
            return block
    raise AssertionError(f"no block starting {head}")


@pytest.fixture(scope="module")
def tree() -> list[Block]:
    return parse(CADDYFILE.read_text())


# ------------------------------------------------------------------------------ static checks
def test_the_api_owns_exactly_its_prefixes_and_the_admin_ui_is_not_one(tree: list[Block]) -> None:
    site = find(tree, "(site)")
    matcher = find(site[1], "@api")[0]
    expect(matcher[1] == "path" and tuple(matcher[2:]) == API_PATHS, matcher)
    expect("/admin/*" not in CADDYFILE.read_text(), "a catch-all /admin/* would take the admin UI from web")
    admin = find(tree, "(admin_site)")
    expect(find(admin[1], "handle", "/admin/v1/*")[1][0][0] == ["import", "to_api"], admin)


def test_every_api_route_is_under_a_prefix_caddy_sends_to_the_api() -> None:
    """If the API gains a path outside these prefixes, production would send it to web (404)."""
    from services.api.app import app

    prefixes = tuple(p.rstrip("*") for p in API_PATHS)
    stray = [path for path in app.openapi()["paths"] if not path.startswith(prefixes)]
    expect(stray == [], f"API paths Caddy would send to web: {stray}")


def test_hosts_match_the_cloudflare_records_in_dns_tf(tree: list[Block]) -> None:
    dns = DNS_TF.read_text()
    # dns.tf: app `@` in production else `<env>`; admin `admin` else `admin-<env>`; www production only.
    expect('var.environment == "production" ? "@" : var.environment' in dns, "app record naming changed")
    expect('var.environment == "production" ? "admin" : "admin-${var.environment}"' in dns, "admin naming")
    expect('local.create_dns && var.environment == "production"' in dns, "www is production-only")
    expect('var.environment == "production" ? "api" : "api-${var.environment}"' in dns, "api naming")
    for environment, expected in HOSTS.items():
        block = find(tree, f"(hosts_{environment})")
        served = {tokens[0].replace("{$DOMAIN}", DOMAIN): children for tokens, children in block[1]}
        expect(set(served) == set(expected), f"{environment}: {sorted(served)}")
        for host, role in expected.items():
            first = served[host][0][0]
            wanted = {
                "site": ["import", "site"],
                "admin": ["import", "admin_site"],
                "api": ["import", "api_site"],
            }.get(role)
            if wanted is None:
                expect(first == ["redir", "https://{$DOMAIN}{uri}", "permanent"], first)
            else:
                expect(first == wanted, f"{host}: {first}")
    expect(find(tree, "import")[0] == ["import", "hosts_{$ENVIRONMENT}"], "hosts chosen by ENVIRONMENT")


def test_trusted_proxies_are_exactly_cloudflares_published_ranges(tree: list[Block]) -> None:
    servers = find(find(tree)[1], "servers")  # the global options block has no head tokens
    trusted = find(servers[1], "trusted_proxies")[0]
    expect(trusted[1] == "static" and trusted[2:] == CLOUDFLARE_RANGES, trusted)
    for cidr in trusted[2:]:
        network = ipaddress.ip_network(cidr)
        expect(not network.is_private and not network.is_loopback, f"{cidr} is not a public edge range")
    expect(find(servers[1], "client_ip_headers")[0] == ["client_ip_headers", "CF-Connecting-IP"], servers)


def test_every_upstream_gets_the_resolved_client_and_no_internal_header(tree: list[Block]) -> None:
    for name, upstream in (("(to_api)", "api:8000"), ("(to_web)", "web:8001")):
        proxy = find(find(tree, name)[1], "reverse_proxy")
        expect(proxy[0] == ["reverse_proxy", upstream], proxy)
        expect(proxy[1][0][0] == ["header_up", "X-Forwarded-For", "{client_ip}"], proxy)
    common = [tokens for tokens, _ in find(tree, "(common)")[1]]
    expect(["request_header", "-X-Internal-Token"] in common, common)
    expect(["request_header", "-X-Visitor-IP"] in common, common)


# ------------------------------------------------------------------------------ with Caddy
needs_caddy = pytest.mark.skipif(CADDY is None, reason="no caddy binary (set CADDY_BIN or put caddy on PATH)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Echo(BaseHTTPRequestHandler):
    """Answers every request with which upstream it is, the path and the headers it received."""

    name = "?"

    def _answer(self) -> None:
        body = json.dumps(
            {
                "upstream": self.name,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer  # noqa: N815 -- http.server's naming

    def log_message(self, format: str, *args: Any) -> None:
        return


def _stub(name: str) -> tuple[ThreadingHTTPServer, int]:
    handler = type(f"Echo_{name}", (_Echo,), {"name": name})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def _env(tmp: pathlib.Path, environment: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "DOMAIN": DOMAIN,
        "ENVIRONMENT": environment,
    }


def _adapt(tmp: pathlib.Path, environment: str, domain: str = DOMAIN) -> subprocess.CompletedProcess[str]:
    env = _env(tmp, environment) | {"DOMAIN": domain}
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [CADDY or "caddy", "adapt", "--config", str(CADDYFILE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@needs_caddy
def test_caddy_refuses_to_start_without_domain_or_environment(tmp_path: pathlib.Path) -> None:
    for environment, domain in (("", DOMAIN), ("dev", DOMAIN), ("production", "")):
        result = _adapt(tmp_path, environment, domain)
        expect(result.returncode != 0, f"ENVIRONMENT={environment!r} DOMAIN={domain!r} adapted")


@needs_caddy
@pytest.mark.parametrize("environment", ["production", "staging"])
def test_caddy_serves_exactly_the_dns_host_names(tmp_path: pathlib.Path, environment: str) -> None:
    result = _adapt(tmp_path, environment)
    expect(result.returncode == 0, result.stderr)
    server = json.loads(result.stdout)["apps"]["http"]["servers"]["srv0"]
    hosts = {h for route in server["routes"] for m in route["match"] for h in m["host"]}
    expect(hosts == set(HOSTS[environment]), f"{environment}: {sorted(hosts)}")
    expect(server["trusted_proxies"]["ranges"] == CLOUDFLARE_RANGES, server["trusted_proxies"])


class LiveCaddy:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(
        self, host: str, path: str, method: str = "GET", headers: dict[str, str] | None = None
    ) -> httpx.Response:
        with httpx.Client(verify=False, timeout=10.0) as client:  # noqa: S501 -- Caddy's throwaway local CA
            return client.request(
                method,
                f"https://127.0.0.1:{self.port}{path}",
                headers={"Host": host, **(headers or {})},
                extensions={"sni_hostname": host},
            )

    def upstream(self, host: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.request(host, path, **kwargs)
        expect(response.status_code == 200, f"{host}{path}: {response.status_code} {response.text[:200]}")
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Iterator[LiveCaddy]:
    """The real Caddyfile with its upstreams pointed at stubs and a local CA, run by real Caddy.
    `request.param` is True to add loopback to the trusted proxies (standing in for Cloudflare)."""
    trust_loopback: bool = request.param
    tmp = tmp_path_factory.mktemp("caddy-trusted" if trust_loopback else "caddy")
    api, api_port = _stub("api")
    web, web_port = _stub("web")
    http_port, https_port = _free_port(), _free_port()
    text = CADDYFILE.read_text()
    api_line, web_line = "reverse_proxy api:8000 ", "reverse_proxy web:8001 "
    expect(text.count(api_line) == 1 and text.count(web_line) == 1, "upstream addresses moved")
    text = text.replace(api_line, f"reverse_proxy 127.0.0.1:{api_port} ")
    text = text.replace(web_line, f"reverse_proxy 127.0.0.1:{web_port} ")
    local = "".join(
        f"\n\t{option}"
        for option in (
            "admin off",
            "local_certs",
            "skip_install_trust",
            f"http_port {http_port}",
            f"https_port {https_port}",
        )
    )
    local += "\n"
    text = re.sub(r"^\{\n", "{" + local, text, count=1, flags=re.MULTILINE)
    if trust_loopback:
        text = text.replace("trusted_proxies static ", "trusted_proxies static 127.0.0.1/32 ", 1)
    config = tmp / "Caddyfile"
    config.write_text(text)
    log = tmp / "caddy.log"
    with log.open("w") as log_file:
        process = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            [CADDY or "caddy", "run", "--config", str(config), "--adapter", "caddyfile"],
            env=_env(tmp, "production"),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with socket.create_connection(("127.0.0.1", https_port), timeout=0.5):
                    break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError(f"caddy exited: {log.read_text()[-2000:]}") from None
                expect(time.monotonic() < deadline, "caddy did not listen within 20 s")
                time.sleep(0.1)
        yield LiveCaddy(https_port)
    finally:
        process.terminate()
        process.wait(timeout=10)
        api.shutdown()
        web.shutdown()


ROUTES = [
    (DOMAIN, "GET", "/v1/sources", "api"),
    (DOMAIN, "GET", "/v1/proposals/geo?bbox=-125,24,-66,50&zoom=4", "api"),
    (DOMAIN, "GET", "/admin/v1/records", "api"),
    (DOMAIN, "GET", "/feeds/proposals.rss", "api"),
    (DOMAIN, "GET", "/feeds/saved/abc123", "api"),
    (DOMAIN, "POST", "/webhooks/stripe", "api"),
    (DOMAIN, "GET", "/", "web"),
    (DOMAIN, "GET", "/proposals", "web"),
    (DOMAIN, "GET", "/api/proposals/geo", "web"),
    (DOMAIN, "GET", "/admin", "web"),
    (DOMAIN, "GET", "/admin/sources", "web"),
    (DOMAIN, "GET", "/admin/records", "web"),
    (DOMAIN, "GET", "/admin/keys", "web"),
    (DOMAIN, "GET", "/login", "web"),
    (f"admin.{DOMAIN}", "GET", "/admin", "web"),
    (f"admin.{DOMAIN}", "GET", "/admin/sources", "web"),
    (f"admin.{DOMAIN}", "GET", "/admin/engagement", "web"),
    (f"admin.{DOMAIN}", "GET", "/login?next=/admin", "web"),
    (f"admin.{DOMAIN}", "POST", "/login", "web"),
    (f"admin.{DOMAIN}", "GET", "/static/css/site.css", "web"),
    (f"admin.{DOMAIN}", "GET", "/admin/v1/records", "api"),
    (f"api.{DOMAIN}", "GET", "/v1/proposals", "api"),
    (f"api.{DOMAIN}", "GET", "/v1/alerts/unsubscribe?token=abc", "api"),
    (f"api.{DOMAIN}", "GET", "/v1/exports/exp_1/download", "api"),
    (f"api.{DOMAIN}", "POST", "/webhooks/attio", "api"),
    (f"api.{DOMAIN}", "POST", "/webhooks/stripe", "api"),
    (f"api.{DOMAIN}", "GET", "/feeds/events.rss", "api"),
    (f"api.{DOMAIN}", "GET", "/errors/rate_limited", "api"),
    (f"api.{DOMAIN}", "GET", "/openapi.json", "api"),
]


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
@pytest.mark.parametrize(("host", "method", "path", "upstream"), ROUTES)
def test_each_route_reaches_its_upstream(
    live: LiveCaddy, host: str, method: str, path: str, upstream: str
) -> None:
    body = live.upstream(host, path, method=method)
    expect(body["upstream"] == upstream, f"{method} {host}{path} went to {body['upstream']}")
    expect(body["path"] == path, f"path rewritten: {body['path']}")
    expect(body["headers"]["host"] == host, body["headers"])


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
def test_redirects(live: LiveCaddy) -> None:
    www = live.request(f"www.{DOMAIN}", "/proposals?state=US-TX")
    expect(www.status_code == 301, www.status_code)
    expect(www.headers["location"] == f"https://{DOMAIN}/proposals?state=US-TX", www.headers)
    admin_root = live.request(f"admin.{DOMAIN}", "/")
    expect(admin_root.status_code == 302 and admin_root.headers["location"] == "/admin", admin_root.headers)


SPOOF = {
    "X-Forwarded-For": "198.51.100.66",
    "CF-Connecting-IP": "198.51.100.77",
    "X-Internal-Token": "guessed",
    "X-Visitor-IP": "198.51.100.88",
}


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
@pytest.mark.parametrize("path", ["/v1/sources", "/login"])
def test_a_direct_client_cannot_name_its_own_address(live: LiveCaddy, path: str) -> None:
    """A peer outside Cloudflare's ranges (anyone reaching the origin directly) is the address the
    upstream sees, whatever it claims; internal headers never cross Caddy."""
    headers = live.upstream(DOMAIN, path, headers=SPOOF)["headers"]
    expect(headers["x-forwarded-for"] == "127.0.0.1", headers)
    expect("x-internal-token" not in headers and "x-visitor-ip" not in headers, headers)
    expect(headers["x-forwarded-proto"] == "https", headers)


@needs_caddy
@pytest.mark.parametrize("live", [True], indirect=True)
@pytest.mark.parametrize("path", ["/v1/sources", "/login"])
def test_a_trusted_edge_passes_only_cf_connecting_ip(live: LiveCaddy, path: str) -> None:
    """With the peer in the trusted ranges (loopback standing in for Cloudflare), the upstream gets
    CF-Connecting-IP and only that: the client-supplied X-Forwarded-For is dropped, not appended."""
    headers = live.upstream(DOMAIN, path, headers=SPOOF)["headers"]
    expect(headers["x-forwarded-for"] == "198.51.100.77", headers)
    two = live.upstream(DOMAIN, path, headers={"CF-Connecting-IP": "2001:db8::7"})["headers"]
    expect(two["x-forwarded-for"] == "2001:db8::7", two)
    expect("x-internal-token" not in headers and "x-visitor-ip" not in headers, headers)
