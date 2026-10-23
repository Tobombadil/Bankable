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
  `X-Forwarded-For`, that internal headers are stripped, and that no log line carries an
  invitation token while every other field is what the unfiltered log wrote (docs/19 §5.3).
  Skipped, not faked, without it.

Checks use `expect` instead of bare `assert` (ruff S101, see infra/scheduler/test_jobs.py).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
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
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "infra" / "compose" / "Caddyfile"
DNS_TF = ROOT / "infra" / "terraform" / "dns.tf"
CADDY = os.environ.get("CADDY_BIN") or shutil.which("caddy")
DOMAIN = "example.test"
TILE_URL = "https://tiles.example.test/basemap.pmtiles"

#: The API owns exactly these prefixes; everything else is the web app (admin UI included).
API_PATHS = ("/v1/*", "/admin/v1/*", "/feeds/*", "/webhooks/*")
#: The site host sends these to the API; `/admin/v1/*` is the admin host's alone (2026-10-10).
SITE_API_PATHS = ("/v1/*", "/feeds/*", "/webhooks/*")
#: Every admin path the site and api hosts refuse with a 404 (`no_admin`).
ADMIN_PATHS = ("/admin", "/admin/*")

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


def global_options(tree: list[Block]) -> Block:
    """The global options block, the one block with no head tokens. A snippet may come before it
    (the invitation-token filter does, so the global options can import it)."""
    blocks = [block for block in tree if block[0] == []]
    expect(len(blocks) == 1, f"one global options block expected, found {len(blocks)}")
    return blocks[0]


def walk(tree: list[Block]) -> Iterator[Block]:
    for block in tree:
        yield block
        yield from walk(block[1])


@pytest.fixture(scope="module")
def tree() -> list[Block]:
    return parse(CADDYFILE.read_text())


# ------------------------------------------------------------------------------ static checks
def test_the_api_owns_exactly_its_prefixes_and_the_admin_ui_is_not_one(tree: list[Block]) -> None:
    site = find(tree, "(site)")
    matcher = find(site[1], "@api")[0]
    expect(matcher[1] == "path" and tuple(matcher[2:]) == SITE_API_PATHS, matcher)
    admin = find(tree, "(admin_site)")
    expect(find(admin[1], "handle", "/admin/v1/*")[1][0][0] == ["import", "to_api"], admin)
    # The admin UI is the web app's: on the admin host nothing sends /admin/<page> anywhere else.
    expect(["import", "no_admin"] not in [tokens for tokens, _ in admin[1]], "admin host must serve admin")
    expect(set(SITE_API_PATHS) | {"/admin/v1/*"} == set(API_PATHS), "together they route every API prefix")


def test_admin_is_a_404_on_the_site_and_api_hosts_before_any_upstream(tree: list[Block]) -> None:
    """docs/51 §2.4 item 5: /admin/v1/* went to the API on the apex, and the admin UI was served by
    web on every host. Both are now the admin host's alone (D-16)."""
    no_admin = find(tree, "(no_admin)")
    matcher = find(no_admin[1], "@admin")[0]
    expect(matcher[1] == "path" and tuple(matcher[2:]) == ADMIN_PATHS, matcher)
    expect(find(no_admin[1], "handle", "@admin")[1] == [(["respond", "404"], [])], no_admin)
    for name in ("(site)", "(api_site)"):
        lines = [tokens for tokens, _ in find(tree, name)[1]]
        expect(lines[:2] == [["import", "common"], ["import", "no_admin"]], f"{name}: {lines[:2]}")
    tokens = [tok for line in _lines(CADDYFILE.read_text()) for tok in line]  # comments removed
    expect(tokens.count("/admin/*") == 1, "only no_admin may name /admin/*")


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
                lines = [tokens for tokens, _ in served[host]]
                expect(
                    lines
                    == [["import", "security_headers"], ["redir", "https://{$DOMAIN}{uri}", "permanent"]],
                    lines,
                )
            else:
                expect(first == wanted, f"{host}: {first}")
    expect(find(tree, "import")[0] == ["import", "hosts_{$ENVIRONMENT}"], "hosts chosen by ENVIRONMENT")


def test_trusted_proxies_are_exactly_cloudflares_published_ranges(tree: list[Block]) -> None:
    servers = find(global_options(tree)[1], "servers")
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


# ------------------------------------------------------------------------------ security headers
WEB = ROOT / "web"
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Reporting-Endpoints": 'csp="/csp-report"',
}
REPORT_ONLY = "Content-Security-Policy-Report-Only"
#: The only rule enforced while `CSP_MODE=report` (the default).
FRAME_ONLY = "frame-ancestors 'none'"
#: The snippet line that hands the policy to `csp_report` or `csp_enforce`.
POLICY_IMPORT = "csp_{$CSP_MODE:report}"


def _headers(tree: list[Block]) -> dict[str, str]:
    """The `(security_headers)` snippet's fields, `?` (write only when absent) stripped. Each is its
    own `header` line: in one `header { }` block Caddy gives every `?` field one shared condition
    (all of them absent), so an upstream sending any one of them dropped all the others."""
    fields = {}
    for tokens, children in find(tree, "(security_headers)")[1]:
        if tokens[0] != "header":
            continue
        expect(not children and len(tokens) == 3, f"one default per header line: {tokens}")
        expect(tokens[1].startswith("?"), f"every field is a default: {tokens}")
        fields[tokens[1][1:]] = tokens[2].strip("`")
    return fields


def _policy_text(tree: list[Block]) -> str:
    imports = [t for t, _ in find(tree, "(security_headers)")[1] if t[:2] == ["import", POLICY_IMPORT]]
    expect(len(imports) == 1 and len(imports[0]) == 3, f"one policy, passed to the mode's snippet: {imports}")
    return str(imports[0][2])


def _policy(value: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for directive in value.split(";"):
        parts = directive.split()
        if parts:
            out[parts[0]] = parts[1:]
    return out


def _style_attributes() -> list[str]:
    """Templates that still carry a `style` attribute (web/templates; none should, docs/60 §2)."""
    return sorted(
        str(t.relative_to(WEB))
        for t in (WEB / "templates").rglob("*.html")
        if re.search(r"\sstyle=\"", t.read_text())
    )


def _inline_script_hashes() -> set[str]:
    """CSP hashes of every executable inline script in the templates. JSON data blocks
    (`application/json`, `application/ld+json`) are not scripts the browser runs."""
    hashes = set()
    for template in sorted((WEB / "templates").rglob("*.html")):
        for match in re.finditer(
            r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", template.read_text(), re.S
        ):
            attrs = match.group("attrs")
            if "src=" in attrs or re.search(r'type="application/(ld\+)?json"', attrs):
                continue
            digest = hashlib.sha256(match.group("body").encode()).digest()
            hashes.add(f"'sha256-{base64.b64encode(digest).decode()}'")
    return hashes


def test_every_host_sends_the_security_headers(tree: list[Block]) -> None:
    """docs/51 §2.4 item 5: no HSTS, nosniff, referrer policy or CSP anywhere before 2026-10-10."""
    expect(_headers(tree) == SECURITY_HEADERS, _headers(tree))
    expect(bool(_policy_text(tree)), "the policy is passed to the CSP_MODE snippet")
    common = [tokens for tokens, _ in find(tree, "(common)")[1]]
    expect(common[0] == ["import", "security_headers"], common)
    for environment in HOSTS:
        for tokens, children in find(tree, f"(hosts_{environment})")[1]:
            first = children[0][0]
            # Each host imports a role that imports `common`, or (the www redirect) the headers.
            expect(
                first
                in (
                    ["import", "site"],
                    ["import", "admin_site"],
                    ["import", "api_site"],
                    ["import", "security_headers"],
                ),
                f"{tokens}: {first}",
            )
    for role in ("(site)", "(admin_site)", "(api_site)"):
        expect(find(tree, role)[1][0][0] == ["import", "common"], role)


def test_csp_mode_picks_report_only_or_enforced_for_the_same_policy(tree: list[Block]) -> None:
    """docs/64 §5: `report` (the default) enforces only `frame-ancestors 'none'` and reports the
    rest; `enforce` enforces the whole policy. Both modes send the one policy string."""
    report = [tokens for tokens, _ in find(tree, "(csp_report)")[1]]
    enforce = [tokens for tokens, _ in find(tree, "(csp_enforce)")[1]]
    expect(
        report
        == [
            ["header", "?Content-Security-Policy", FRAME_ONLY],
            ["header", f"?{REPORT_ONLY}", "{args[0]}"],
        ],
        report,
    )
    expect(enforce == [["header", "?Content-Security-Policy", "{args[0]}"]], enforce)


def test_the_policy_allows_the_site_as_it_is(tree: list[Block]) -> None:
    """Derived from web/, so a new CDN package, inline script or third-party origin fails here
    instead of turning into reports or, once enforced, a broken page."""
    from web.assets import CDN_ASSETS
    from web.csp_reports import REPORT_PATH

    policy = _policy(_policy_text(tree))
    packages = {name: asset["url"].split("/dist/")[0] + "/" for name, asset in CDN_ASSETS.items()}
    expect(all(url.startswith("https://cdn.jsdelivr.net/npm/") for url in packages.values()), packages)
    turnstile = re.search(r'TURNSTILE_SCRIPT = "(https://[^/"]+)/', (WEB / "reports.py").read_text())
    expect(turnstile is not None, "web/reports.py TURNSTILE_SCRIPT moved")
    turnstile_origin = turnstile.group(1) if turnstile else ""
    basemap = (WEB / "static" / "js" / "basemap.js").read_text()
    protomaps = "https://protomaps.github.io/basemaps-assets/"
    expect(f'"{protomaps}fonts/' in basemap and f'"{protomaps}sprites/' in basemap, "glyph/sprite host moved")
    hashes = _inline_script_hashes()
    expect(len(hashes) == 1, f"one inline script today (base.html's js class): {hashes}")
    scripts = {packages[n] for n in ("maplibre_js", "pmtiles_js", "basemaps_js")}
    expect(set(policy["script-src"]) == {"'self'", *scripts, turnstile_origin, *hashes}, policy["script-src"])
    expect(set(policy["style-src"]) == {"'self'", packages["maplibre_css"]}, policy["style-src"])
    expect(set(policy["img-src"]) == {"'self'", "data:", "blob:", protomaps, "{$MAP_TILE_URL}"}, policy)
    expect(set(policy["connect-src"]) == {"'self'", protomaps, "{$MAP_TILE_URL}"}, policy)
    expect(policy["frame-src"] == [turnstile_origin] and policy["font-src"] == ["'self'"], policy)
    expect(policy["worker-src"] == ["'self'", "blob:"] and policy["object-src"] == ["'none'"], policy)
    expect(policy["default-src"] == ["'self'"] and policy["base-uri"] == ["'self'"], policy)
    expect(policy["frame-ancestors"] == ["'none'"], "the enforced policy keeps the framing rule")
    expect(policy["report-uri"] == [REPORT_PATH] and policy["report-to"] == ["csp"], policy)
    expect(SECURITY_HEADERS["Reporting-Endpoints"] == f'csp="{REPORT_PATH}"', "report-to names this endpoint")
    expect("'unsafe-eval'" not in _policy_text(tree), "nothing on the site evaluates strings")
    base = (WEB / "templates" / "base.html").read_text()
    expect("fonts.googleapis.com" not in base and "fonts.gstatic.com" not in base, "fonts are self-hosted")


def test_inline_styles_are_allowed_only_while_a_template_has_a_style_attribute(tree: list[Block]) -> None:
    """A ratchet (2026-10-10). `style-src-attr 'unsafe-inline'` stays only while a template carries
    a `style` attribute; on that day two did, in templates other work owned (home_map.html's
    legend, partials/_org_pipeline.html's status icon), and styles.css already has their classes.
    Once none does, this fails until the allowance leaves the Caddyfile."""
    policy = _policy(_policy_text(tree))
    left = _style_attributes()
    if left:
        expect(policy.get("style-src-attr") == ["'unsafe-inline'"], f"{left} still need it: {policy}")
    else:
        expect("style-src-attr" not in policy, "no template has a style attribute: drop style-src-attr")
    expect(
        "'unsafe-inline'" not in policy["style-src"] + policy["script-src"], "never for <style> or <script>"
    )
    expect(
        set(left) <= {"templates/home_map.html", "templates/partials/_org_pipeline.html"},
        f"a new style attribute: {left}",
    )


def test_caddy_receives_the_basemap_url_its_policy_names_and_the_mode() -> None:
    prod = (ROOT / "infra" / "compose" / "compose.prod.yml").read_text()
    expect(
        "      MAP_TILE_URL: ${MAP_TILE_URL:-}\n" in prod, "compose.prod.yml must pass MAP_TILE_URL to caddy"
    )
    expect("      CSP_MODE: ${CSP_MODE:-report}\n" in prod, "compose.prod.yml must pass CSP_MODE to caddy")


def test_the_report_path_reaches_the_site_on_every_host_and_skips_the_gate(tree: list[Block]) -> None:
    gate = [t for t, _ in find(tree, "(gate_basic)")[1] if t[:1] == ["@gated"]]
    expect(bool(gate) and "/csp-report" in gate[0], f"browsers report without the login: {gate}")
    api = find(tree, "(api_site)")[1]
    handles = [tokens for tokens, _ in api if tokens[0] == "handle"]
    expect(["handle", "/csp-report"] in handles, f"the api host sends reports to web: {handles}")


# ------------------------------------------------------------------------------ the invitation token
TOKEN_FILTER = "invitation_token_redacted"  # noqa: S105 -- a snippet name, not a credential
#: Where the token can appear in a Caddy line (docs/19 §5.3; §6.2 for the URL shape).
TOKEN_FIELDS = (
    "request>uri",
    "request>headers>Referer",
    "request>headers>Hx-Current-Url",  # htmx's page URL; Go writes header names in this case
    "resp_headers>Location",
)
TOKEN_REPLACEMENT = "${1}${2}REDACTED"  # noqa: S105 -- a regexp replacement, not a credential
#: `secrets.token_urlsafe(32)`'s shape (docs/19 §2.6 rule 2), recognisable in a log.
TOKEN = "SENTINELtok_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P"  # noqa: S105 -- a test sentinel, asserted absent from the logs


def _token_filter(tree: list[Block]) -> list[list[str]]:
    """The snippet's field lines, after checking it wraps the stock JSON encoder and nothing else."""
    snippet = find(tree, f"({TOKEN_FILTER})")[1]
    expect([tokens for tokens, _ in snippet] == [["format", "filter"]], snippet)
    encoder = snippet[0][1]
    expect([tokens for tokens, _ in encoder] == [["wrap", "json"], ["fields"]], encoder)
    return [tokens for tokens, _ in find(encoder, "fields")[1]]


def test_every_log_filters_the_invitation_token_through_one_snippet(tree: list[Block]) -> None:
    """The access log (`common`, every site, admin and api host) and Caddy's default logger (its
    error lines carry the request too) both import the one filter; no log writes its own format."""
    from infra.logging_config import INVITATION_TOKEN_PATTERN

    lines = _token_filter(tree)
    expect([line[0] for line in lines] == list(TOKEN_FIELDS), lines)
    for line in lines:
        expect(line[1:] == ["regexp", INVITATION_TOKEN_PATTERN, TOKEN_REPLACEMENT], line)
    logs = {tuple(head): [tokens for tokens, _ in body] for head, body in walk(tree) if head[:1] == ["log"]}
    expect(
        logs
        == {
            ("log", "default"): [["output", "stderr"], ["import", TOKEN_FILTER]],
            ("log",): [["output", "stdout"], ["import", TOKEN_FILTER]],
        },
        logs,
    )
    expect(find(global_options(tree)[1], "log", "default") is not None, "the default logger is configured")
    expect(["log"] in [head for head, _ in find(tree, "(common)")[1]], "the access log lives in common")
    formats = [head for head, _ in walk(tree) if head[:1] == ["format"]]
    expect(formats == [["format", "filter"]], f"only the filter snippet names a format: {formats}")


def _caddy_replace(text: str) -> str:
    """What Caddy's regexp filter writes: Go's `${n}` expansion, done with Python's `re` (the two
    engines agree on this pattern: no backreferences, no lookaround)."""
    tree = parse(CADDYFILE.read_text())
    _, _, pattern, replacement = _token_filter(tree)[0]
    return re.sub(pattern, re.sub(r"\$\{(\d+)\}", r"\\g<\1>", replacement), text)


TOKEN_SHAPES = [
    (f"/my/invitations/accept?t={TOKEN}", "/my/invitations/accept?t=REDACTED"),
    (f"/my/invitations/accept?x=1&t={TOKEN}&y=2", "/my/invitations/accept?x=1&t=REDACTED&y=2"),
    (
        f"https://{DOMAIN}/my/invitations/accept?t={TOKEN}#accept",
        f"https://{DOMAIN}/my/invitations/accept?t=REDACTED#accept",
    ),
    (
        f"/login?next=%2Fmy%2Finvitations%2Faccept%3Ft%3D{TOKEN}&e=1",
        "/login?next=%2Fmy%2Finvitations%2Faccept%3Ft%3DREDACTED&e=1",
    ),
    (
        f"/login?next=%2Fmy%2Finvitations%2Faccept%3Fx%3D1%26t%3D{TOKEN}%26y%3D2",
        "/login?next=%2Fmy%2Finvitations%2Faccept%3Fx%3D1%26t%3DREDACTED%26y%3D2",
    ),
    (
        f"/register?next=%252Fmy%252Finvitations%252Faccept%253Ft%253D{TOKEN}",
        "/register?next=%252Fmy%252Finvitations%252Faccept%253Ft%253DREDACTED",
    ),
    (f"/my/invitations/{TOKEN}", "/my/invitations/REDACTED"),
    (f"/login?next=%2Fmy%2Finvitations%2F{TOKEN}", "/login?next=%2Fmy%2Finvitations%2FREDACTED"),
    (f"/MY/Invitations/accept?T={TOKEN}", "/MY/Invitations/accept?T=REDACTED"),
    # `%74` is `t`: the app decodes the name, so this is the token too; raw and inside `next`.
    (f"/my/invitations/accept?%74={TOKEN}", "/my/invitations/accept?%74=REDACTED"),
    (f"/my/invitations/accept?x=1&%54%3D{TOKEN}", "/my/invitations/accept?x=1&%54%3DREDACTED"),
    (
        f"/login?next=%2Fmy%2Finvitations%2Faccept%3F%2574%3D{TOKEN}",
        "/login?next=%2Fmy%2Finvitations%2Faccept%3F%2574%3DREDACTED",
    ),
    (f"/my/invitations/accept?x=1;t={TOKEN};y=2", "/my/invitations/accept?x=1;t=REDACTED;y=2"),
    # Left alone: other parameters and paths, including opaque ids under /my/ and /v1/my/.
    ("/v1/proposals?state=US-TX&at=5&t2=x&st=1", "/v1/proposals?state=US-TX&at=5&t2=x&st=1"),
    ("/v1/proposals?%74x=1&%2574y=2&q=%74", "/v1/proposals?%74x=1&%2574y=2&q=%74"),
    ("/my/invitations/accept", "/my/invitations/accept"),
    ("/my/projects/prj_01HABCDEFGHJKMNPQRSTVW", "/my/projects/prj_01HABCDEFGHJKMNPQRSTVW"),
    (
        "/v1/my/projects/prj_01HABCDEFGHJKMNPQRSTVW/invitations/inv_01HABCDEFGHJKMNPQRSTVWXYZ0",
        "/v1/my/projects/prj_01HABCDEFGHJKMNPQRSTVW/invitations/inv_01HABCDEFGHJKMNPQRSTVWXYZ0",
    ),
    ("/feeds/saved/abc123", "/feeds/saved/abc123"),
]


@pytest.mark.parametrize(("logged", "written"), TOKEN_SHAPES)
def test_the_token_pattern_redacts_every_invitation_url_shape(logged: str, written: str) -> None:
    expect(_caddy_replace(logged) == written, f"{logged} -> {_caddy_replace(logged)}")


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
        if self.headers.get("X-Test-Upstream-Drop"):  # an upstream that dies mid-request: a 502
            self.close_connection = True
            return
        body = json.dumps(
            {
                "upstream": self.name,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        ).encode()
        # An upstream redirect. The target travels base64-encoded, so the test's own request header
        # (Caddy logs every request header) never carries the token it checks for.
        encoded = self.headers.get("X-Test-Upstream-Location")
        location = base64.urlsafe_b64decode(encoded).decode() if encoded else None
        self.send_response(302 if location else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        if self.headers.get("X-Test-Upstream-Referrer-Policy"):  # an upstream with its own value
            self.send_header("Referrer-Policy", self.headers["X-Test-Upstream-Referrer-Policy"])
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
        "MAP_TILE_URL": TILE_URL,
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
    def __init__(self, port: int, log: pathlib.Path) -> None:
        self.port = port
        self.log = log  # Caddy's stdout (the access log) and stderr (its default logger)

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


@contextlib.contextmanager
def _running_caddy(
    tmp: pathlib.Path,
    *,
    trust_loopback: bool,
    extra_env: dict[str, str] | None = None,
    edit: Callable[[str], str] | None = None,
) -> Iterator[LiveCaddy]:
    """The real Caddyfile with its upstreams pointed at stubs and a local CA, run by real Caddy.
    `trust_loopback` adds loopback to the trusted proxies (standing in for Cloudflare); `extra_env`
    reaches the Caddyfile's `{$...}` placeholders (the access gate's three keys); `edit` rewrites
    the file's text first (a reference configuration to compare against)."""
    api, api_port = _stub("api")
    web, web_port = _stub("web")
    http_port, https_port = _free_port(), _free_port()
    text = CADDYFILE.read_text() if edit is None else edit(CADDYFILE.read_text())
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
            env=_env(tmp, "production") | (extra_env or {}),
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
        caddy = LiveCaddy(https_port, log)
        # Listening is not ready: Caddy issues each host's local certificate after it binds, and a
        # handshake before that fails with an internal-error alert (seen 2026-10-10 as a flaky first
        # request). Wait until every host completes a TLS handshake; any HTTP status will do.
        for host in HOSTS["production"]:
            while True:
                try:
                    caddy.request(host, "/health")
                    break
                except httpx.ConnectError:
                    expect(time.monotonic() < deadline, f"no certificate for {host} within 20 s")
                    time.sleep(0.1)
        yield caddy
    finally:
        process.terminate()
        process.wait(timeout=10)
        api.shutdown()
        web.shutdown()


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Iterator[LiveCaddy]:
    """`request.param` is True to add loopback to the trusted proxies (standing in for Cloudflare)."""
    trust_loopback: bool = request.param
    tmp = tmp_path_factory.mktemp("caddy-trusted" if trust_loopback else "caddy")
    with _running_caddy(tmp, trust_loopback=trust_loopback) as caddy:
        yield caddy


GATE_USER, GATE_PASSWORD = "beta", "correct horse battery staple"


@pytest.fixture(scope="module")
def gated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveCaddy]:
    """The private-beta access gate (docs/64): `SITE_ACCESS=basic` with a hash made the way the
    runbook makes it, `caddy hash-password` then base64."""
    hashed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [CADDY or "caddy", "hash-password", "--plaintext", GATE_PASSWORD],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    env = {
        "SITE_ACCESS": "basic",
        "SITE_ACCESS_USER": GATE_USER,
        "SITE_ACCESS_HASH": base64.b64encode(hashed.encode()).decode(),
    }
    with _running_caddy(tmp_path_factory.mktemp("caddy-gated"), trust_loopback=False, extra_env=env) as caddy:
        yield caddy


ROUTES = [
    (DOMAIN, "GET", "/v1/sources", "api"),
    (DOMAIN, "GET", "/v1/proposals/geo?bbox=-125,24,-66,50&zoom=4", "api"),
    (DOMAIN, "GET", "/feeds/proposals.rss", "api"),
    (DOMAIN, "GET", "/feeds/saved/abc123", "api"),
    (DOMAIN, "POST", "/webhooks/stripe", "api"),
    (DOMAIN, "GET", "/", "web"),
    (DOMAIN, "GET", "/proposals", "web"),
    (DOMAIN, "GET", "/api/proposals/geo", "web"),
    (DOMAIN, "GET", "/administration-costs", "web"),  # only /admin and /admin/* are refused
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
    (DOMAIN, "POST", "/csp-report", "web"),
    (f"admin.{DOMAIN}", "POST", "/csp-report", "web"),
    (f"api.{DOMAIN}", "POST", "/csp-report", "web"),
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


ADMIN_ELSEWHERE = [
    (host, method, path)
    for host in (DOMAIN, f"api.{DOMAIN}")
    for method, path in (
        ("GET", "/admin"),
        ("GET", "/admin/"),
        ("GET", "/admin/sources"),
        ("GET", "/Admin/keys"),
        ("GET", "/admin/v1/records"),
        ("POST", "/admin/v1/sources/x/run"),
    )
]


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
@pytest.mark.parametrize(("host", "method", "path"), ADMIN_ELSEWHERE)
def test_admin_is_a_404_off_the_admin_host(live: LiveCaddy, host: str, method: str, path: str) -> None:
    response = live.request(host, path, method)
    expect(
        response.status_code == 404, f"{method} {host}{path}: {response.status_code} {response.text[:200]}"
    )
    expect("upstream" not in response.text, "no upstream may answer an admin path off the admin host")
    expect(response.headers.get("x-content-type-options") == "nosniff", response.headers)


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
@pytest.mark.parametrize(
    ("host", "path"),
    [
        (DOMAIN, "/proposals"),
        (DOMAIN, "/v1/sources"),
        (f"admin.{DOMAIN}", "/admin"),
        (f"api.{DOMAIN}", "/v1/proposals"),
    ],
)
def test_every_response_carries_the_security_headers(live: LiveCaddy, host: str, path: str) -> None:
    response = live.request(host, path)
    expect(response.status_code == 200, response.status_code)
    for name, value in SECURITY_HEADERS.items():
        expect(response.headers.get(name) == value, f"{host}{path} {name}: {response.headers.get(name)}")
    # CSP_MODE unset: report mode. Only the framing rule is enforced; the whole policy reports.
    expect(response.headers.get_list("Content-Security-Policy") == [FRAME_ONLY], response.headers)
    policy = _policy(response.headers.get(REPORT_ONLY, ""))
    expect(TILE_URL in policy["connect-src"] and TILE_URL in policy["img-src"], policy)
    expect(policy["report-uri"] == ["/csp-report"] and policy["frame-ancestors"] == ["'none'"], policy)


@pytest.fixture(scope="module")
def enforced(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveCaddy]:
    tmp = tmp_path_factory.mktemp("caddy-enforced")
    with _running_caddy(tmp, trust_loopback=False, extra_env={"CSP_MODE": "enforce"}) as caddy:
        yield caddy


@needs_caddy
@pytest.mark.parametrize(("host", "path"), [(DOMAIN, "/proposals"), (f"admin.{DOMAIN}", "/admin")])
def test_csp_mode_enforce_sends_the_whole_policy_as_the_enforced_one(
    enforced: LiveCaddy, host: str, path: str
) -> None:
    response = enforced.request(host, path)
    expect(response.status_code == 200, response.status_code)
    [header] = response.headers.get_list("Content-Security-Policy")
    policy = _policy(header)
    expect(
        policy == _policy(_policy_text(parse(CADDYFILE.read_text())).replace("{$MAP_TILE_URL}", TILE_URL)),
        policy,
    )
    expect(REPORT_ONLY not in response.headers, "enforce sends no report-only copy")
    expect(response.headers.get("Reporting-Endpoints") == 'csp="/csp-report"', response.headers)


@needs_caddy
@pytest.mark.parametrize("mode", ["", "Enforce", "off", "report-only"])
def test_caddy_refuses_a_csp_mode_it_does_not_know(tmp_path: pathlib.Path, mode: str) -> None:
    env = _env(tmp_path, "production") | {"CSP_MODE": mode}
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [CADDY or "caddy", "adapt", "--config", str(CADDYFILE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    expect(result.returncode != 0, f"CSP_MODE={mode!r} adapted")


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
def test_the_redirects_carry_them_too_and_an_upstream_value_wins(live: LiveCaddy) -> None:
    for host, path in ((f"www.{DOMAIN}", "/proposals"), (f"admin.{DOMAIN}", "/")):
        redirect = live.request(host, path)
        expect(redirect.status_code in (301, 302), redirect.status_code)
        expect(redirect.headers.get("strict-transport-security") == "max-age=31536000", redirect.headers)
    own = live.request(DOMAIN, "/proposals", headers={"X-Test-Upstream-Referrer-Policy": "no-referrer"})
    expect(own.headers.get_list("Referrer-Policy") == ["no-referrer"], own.headers)
    # An upstream's own value replaces that one default only. Until 2026-10-10 the defaults shared one
    # condition (all absent), so this response would have carried no HSTS, nosniff or CSP at all.
    for name, value in SECURITY_HEADERS.items():
        if name != "Referrer-Policy":
            expect(own.headers.get(name) == value, f"{name} dropped beside an upstream Referrer-Policy")
    expect(
        own.headers.get("Content-Security-Policy") == FRAME_ONLY and REPORT_ONLY in own.headers, own.headers
    )


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


GATED_PAGES = [
    (DOMAIN, "GET", "/", "web"),
    (DOMAIN, "GET", "/proposals", "web"),
    (DOMAIN, "GET", "/v1/sources", "api"),
    (DOMAIN, "GET", "/feeds/proposals.rss", "api"),
    (f"admin.{DOMAIN}", "GET", "/admin", "web"),
    (f"api.{DOMAIN}", "GET", "/v1/proposals", "api"),
]


def _basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@needs_caddy
@pytest.mark.parametrize(("host", "method", "path", "upstream"), GATED_PAGES)
def test_the_beta_gate_asks_for_the_login_and_forwards_none_of_it(
    gated: LiveCaddy, host: str, method: str, path: str, upstream: str
) -> None:
    for headers in ({}, _basic(GATE_USER, "wrong"), _basic("someone", GATE_PASSWORD)):
        refused = gated.request(host, path, method, headers=headers)
        expect(refused.status_code == 401, f"{host}{path} with {headers}: {refused.status_code}")
        expect(refused.headers.get("www-authenticate", "").startswith("Basic"), refused.headers)
    answered = gated.request(host, path, method, headers=_basic(GATE_USER, GATE_PASSWORD))
    expect(answered.status_code == 200, f"{host}{path}: {answered.status_code} {answered.text[:200]}")
    body = answered.json()
    expect(body["upstream"] == upstream, body)
    expect("authorization" not in body["headers"], "the beta login reached the upstream")
    expect(answered.headers.get("x-robots-tag") == "noindex, nofollow", answered.headers)
    expect(answered.headers.get("cache-control") == "private, no-store", answered.headers)


@needs_caddy
@pytest.mark.parametrize("host", [DOMAIN, f"api.{DOMAIN}"])
def test_behind_the_gate_admin_off_the_admin_host_asks_for_the_login_then_404s(
    gated: LiveCaddy, host: str
) -> None:
    refused = gated.request(host, "/admin/v1/records")
    expect(refused.status_code == 401, refused.status_code)
    answered = gated.request(host, "/admin/v1/records", headers=_basic(GATE_USER, GATE_PASSWORD))
    expect(answered.status_code == 404, answered.status_code)
    admin = gated.request(f"admin.{DOMAIN}", "/admin/v1/records", headers=_basic(GATE_USER, GATE_PASSWORD))
    expect(admin.status_code == 200 and admin.json()["upstream"] == "api", admin.status_code)


@needs_caddy
@pytest.mark.parametrize(
    ("host", "method", "path", "upstream"),
    [
        (DOMAIN, "POST", "/webhooks/stripe", "api"),
        (f"api.{DOMAIN}", "POST", "/webhooks/attio", "api"),
        (DOMAIN, "GET", "/health", "web"),
        (f"api.{DOMAIN}", "GET", "/v1/health", "api"),
        (DOMAIN, "POST", "/csp-report", "web"),
        (f"admin.{DOMAIN}", "POST", "/csp-report", "web"),
        (f"api.{DOMAIN}", "POST", "/csp-report", "web"),
    ],
)
def test_the_beta_gate_lets_webhooks_and_health_checks_through(
    gated: LiveCaddy, host: str, method: str, path: str, upstream: str
) -> None:
    body = gated.upstream(host, path, method=method, headers={"Authorization": "Bearer whsec-test"})
    expect(body["upstream"] == upstream, body)
    expect(body["headers"].get("authorization") == "Bearer whsec-test", "an ungated request keeps its own")


@needs_caddy
@pytest.mark.parametrize("access", [None, "open"])
def test_without_site_access_basic_the_gate_is_open(tmp_path: pathlib.Path, access: str | None) -> None:
    env = _env(tmp_path, "production") | ({} if access is None else {"SITE_ACCESS": access})
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [CADDY or "caddy", "adapt", "--config", str(CADDYFILE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    expect(result.returncode == 0, result.stderr)
    expect("authentication" not in result.stdout, f"SITE_ACCESS={access!r} gated the site")


@needs_caddy
@pytest.mark.parametrize("access", ["", "basik", "Basic"])
def test_caddy_refuses_a_site_access_it_does_not_know(tmp_path: pathlib.Path, access: str) -> None:
    """Fail closed: an empty or misspelt value stops Caddy rather than serving the site open
    (Caddy's `{$VAR:default}` covers an unset variable only; compose always passes a value)."""
    env = _env(tmp_path, "production") | {"SITE_ACCESS": access}
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [CADDY or "caddy", "adapt", "--config", str(CADDYFILE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    expect(result.returncode != 0, f"SITE_ACCESS={access!r} adapted")


# ------------------------------------------------------------------------------ the token, in Caddy's logs
DROP = "X-Test-Upstream-Drop"
LOCATION = "X-Test-Upstream-Location"
SIGN_IN_NEXT = f"/login?next=%2Fmy%2Finvitations%2Faccept%3Ft%3D{TOKEN}"
INVITATION_URL = f"https://{DOMAIN}/my/invitations/accept?t={TOKEN}"
#: Request headers that carry a page's URL, as sent and as Caddy's log names them.
URL_HEADERS = {"Referer": "Referer", "HX-Current-URL": "Hx-Current-Url"}


def _redirect_to(target: str) -> dict[str, str]:
    """Ask the stub upstream to answer 302 to `target` (see `_Echo`)."""
    return {LOCATION: base64.urlsafe_b64encode(target.encode()).decode()}


def _with_probe(path: str, probe: str) -> str:
    return f"{path}{'&' if '?' in path else '?'}probe={probe}"


def _entries(log: pathlib.Path, probe: str, *, errors: int) -> list[dict[str, Any]]:
    """The access line, then any error lines, for the one request carrying `probe`, once Caddy has
    written them (it writes after the response has gone)."""
    deadline = time.monotonic() + 5
    while True:
        access: list[dict[str, Any]] = []
        error: list[dict[str, Any]] = []
        for line in log.read_text().splitlines():
            entry = json.loads(line) if line.startswith("{") else {}
            if f"probe={probe}" not in str(entry.get("request", {}).get("uri", "")):
                continue
            logger = str(entry.get("logger", ""))
            (access if logger.startswith("http.log.access.") else error).append(entry)
        if (len(access), len(error)) == (1, errors) or time.monotonic() > deadline:
            expect((len(access), len(error)) == (1, errors), f"lines for {probe}: {access + error}")
            return access + error
        time.sleep(0.05)


LOGGED_TOKEN_CASES = [
    pytest.param(DOMAIN, f"/my/invitations/accept?t={TOKEN}", {}, id="invitation-link"),
    pytest.param(DOMAIN, f"/my/invitations/accept?x=1&t={TOKEN}", {}, id="second-parameter"),
    pytest.param(DOMAIN, SIGN_IN_NEXT, {}, id="sign-in-next"),
    pytest.param(DOMAIN, f"/my/invitations/{TOKEN}", {}, id="path-segment"),
    pytest.param(DOMAIN, f"/my/invitations/accept?%74={TOKEN}", {}, id="percent-encoded-name"),
    pytest.param(DOMAIN, "/static/css/my.css", {"Referer": INVITATION_URL}, id="referer-of-the-next-request"),
    pytest.param(DOMAIN, "/proposals", {"HX-Current-URL": INVITATION_URL}, id="htmx-current-url"),
    pytest.param(DOMAIN, "/my/invitations/accept", _redirect_to(SIGN_IN_NEXT), id="redirect-location"),
    pytest.param(f"admin.{DOMAIN}", f"/my/invitations/accept?t={TOKEN}", {}, id="admin-host"),
    pytest.param(f"api.{DOMAIN}", f"/v1/my/invitations/accept?t={TOKEN}", {}, id="api-host"),
    pytest.param(DOMAIN, f"/my/invitations/accept?t={TOKEN}", {DROP: "1"}, id="upstream-error-line"),
]


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
@pytest.mark.parametrize(("host", "path", "headers"), LOGGED_TOKEN_CASES)
def test_invitation_token_filtered_from_access_log(
    live: LiveCaddy, host: str, path: str, headers: dict[str, str]
) -> None:
    """docs/19 §5.3 and §5.6: a sentinel token sent each way it can reach a Caddy line (the URI,
    the Referer of the page's next request, a redirect's Location, Caddy's own error line for a
    502) is in no line of either log, while the upstream still receives it untouched."""
    probe = uuid.uuid4().hex[:12]
    sent = _with_probe(path, probe)
    response = live.request(host, sent, headers=headers)
    if DROP in headers:
        expect(response.status_code == 502, response.status_code)
    else:
        expect(response.status_code == (302 if LOCATION in headers else 200), response.status_code)
        expect(response.json()["path"] == sent, "the upstream must still receive the token")
    entries = _entries(live.log, probe, errors=1 if DROP in headers else 0)
    expect(TOKEN not in live.log.read_text(), "the token reached Caddy's log")
    for entry in entries:
        request = entry["request"]
        expect(request["uri"] == sent.replace(TOKEN, "REDACTED"), request["uri"])
        for sent_as, logged_as in URL_HEADERS.items():
            if sent_as in headers:
                wanted = [headers[sent_as].replace(TOKEN, "REDACTED")]
                expect(request["headers"][logged_as] == wanted, request["headers"])
    if LOCATION in headers:
        location = entries[0]["resp_headers"]["Location"]
        expect(location == [SIGN_IN_NEXT.replace(TOKEN, "REDACTED")], location)


#: The access log as it was before the token filter: `format json` (until 2026-10-22).
STOCK_SITE_LOG = "\t\toutput stdout\n\t\tformat json\n"


def _stock(text: str) -> str:
    """The Caddyfile with its logs as they were before the filter: the access log `format json`,
    and Caddy's default logger left to Caddy's own default (JSON when not on a terminal)."""
    site = f"\t\toutput stdout\n\t\timport {TOKEN_FILTER}\n"
    default = f"\tlog default {{\n\t\toutput stderr\n\t\timport {TOKEN_FILTER}\n\t}}\n"
    expect(text.count(site) == 1 and text.count(default) == 1, "the log blocks moved; update _stock")
    return text.replace(site, STOCK_SITE_LOG).replace(default, "")


@pytest.fixture(scope="module")
def stock(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveCaddy]:
    with _running_caddy(tmp_path_factory.mktemp("caddy-stock"), trust_loopback=False, edit=_stock) as caddy:
        yield caddy


#: Values that differ between any two requests or Caddy processes, whatever the log format.
VOLATILE = (
    ("ts",),
    ("duration",),
    ("err_id",),
    ("request", "remote_port"),
    ("resp_headers", "Date"),  # the stub's clock
    ("resp_headers", "Alt-Svc"),  # names each process's own port
)


def _stable(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(entry))
    for *parents, key in VOLATILE:
        node = out
        for parent in parents:
            node = node.get(parent) or {}
        node.pop(key, None)
    return out


COMPARED = [
    pytest.param(DOMAIN, "/proposals?state=US-TX", {}, id="public-page"),
    pytest.param(f"api.{DOMAIN}", "/v1/proposals", {"Authorization": "Bearer ik_test"}, id="api-call"),
    pytest.param(f"admin.{DOMAIN}", "/admin", {"Cookie": "session=abc"}, id="admin-page"),
    pytest.param(
        DOMAIN,
        f"/my/invitations/accept?t={TOKEN}",
        {"Referer": INVITATION_URL, "HX-Current-URL": INVITATION_URL},
        id="invitation-link",
    ),
    pytest.param(DOMAIN, "/my/invitations/accept", _redirect_to(SIGN_IN_NEXT), id="redirect"),
    pytest.param(DOMAIN, f"/my/invitations/accept?t={TOKEN}", {DROP: "1"}, id="upstream-error"),
]


@needs_caddy
@pytest.mark.parametrize("live", [False], indirect=True)
@pytest.mark.parametrize(("host", "path", "headers"), COMPARED)
def test_the_logs_are_otherwise_unchanged(
    live: LiveCaddy, stock: LiveCaddy, host: str, path: str, headers: dict[str, str]
) -> None:
    """The same request through the Caddyfile and through its pre-filter twin (`_stock`) gives the
    same access line, and the same error line on the default logger, field for field and in the
    same order, except that the token reads REDACTED."""
    probe = uuid.uuid4().hex[:12]
    sent = _with_probe(path, probe)
    errors = 1 if DROP in headers else 0
    for caddy in (live, stock):
        caddy.request(host, sent, headers=headers)
    ours, before = _entries(live.log, probe, errors=errors), _entries(stock.log, probe, errors=errors)
    for mine, theirs in zip(ours, before, strict=True):
        expect(list(mine) == list(theirs), f"field order changed:\n{list(mine)}\n{list(theirs)}")
        expect(list(mine["request"]) == list(theirs["request"]), "request field order changed")
        wanted = json.loads(json.dumps(_stable(theirs)).replace(TOKEN, "REDACTED"))
        expect(_stable(mine) == wanted, f"\nfiltered:   {_stable(mine)}\nunfiltered: {wanted}")
