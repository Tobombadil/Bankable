"""infra/terraform firewall (docs/51 §2.4 item 5, 2026-10-10): 22/80/443 were open to 0.0.0.0/0 in
literals. The sources are now variables whose defaults keep that behaviour, and the Cloudflare
ranges the web rule can be narrowed to are read from the Caddyfile's trusted_proxies line, the one
place they are vendored. Static checks; `tofu validate` and `tofu console` were run by hand (no
provider download in the test suite). Checks use `expect` instead of bare `assert` (ruff S101)."""

from __future__ import annotations

import ipaddress
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "terraform"
CADDYFILE = ROOT / "infra" / "compose" / "Caddyfile"
OPEN = ["0.0.0.0/0", "::/0"]


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


def _firewall_rules(main: str) -> dict[tuple[str, str], str]:
    """(firewall, port) -> the `source_ips` expression of that rule."""
    rules: dict[tuple[str, str], str] = {}
    for name, body in re.findall(r'resource "hcloud_firewall" "(\w+)" \{(.*?)\n\}', main, re.S):
        for port, sources in re.findall(r'port\s*=\s*"(\d+)"\s*\n\s*source_ips\s*=\s*([^\n#]+)', body):
            rules[(name, port)] = sources.strip()
    return rules


def test_no_firewall_rule_names_a_literal_source() -> None:
    rules = _firewall_rules((TERRAFORM / "main.tf").read_text())
    expect(
        rules
        == {
            ("web", "22"): "var.ssh_source_cidrs",
            ("web", "80"): "local.web_source_cidrs",
            ("web", "443"): "local.web_source_cidrs",
            ("worker", "22"): "var.ssh_source_cidrs",
        },
        rules,
    )


def test_the_defaults_keep_todays_rules_until_the_owner_narrows_them() -> None:
    variables = (TERRAFORM / "variables.tf").read_text()
    for name in ("ssh_source_cidrs", "web_source_cidrs"):
        block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', variables, re.S)
        expect(block is not None, f"{name} missing")
        body = block.group(1) if block else ""
        expect('default     = ["0.0.0.0/0", "::/0"]' in body, f"{name}: default changed")
        expect("validation" in body, f"{name}: no validation")
    expect('"cloudflare"' in variables, "the documented Cloudflare value")
    example = (TERRAFORM / "terraform.tfvars.example").read_text()
    expect('web_source_cidrs     = ["cloudflare"]' in example and "ssh_source_cidrs" in example, example)


def test_the_cloudflare_ranges_come_from_the_caddyfile_and_are_public() -> None:
    """main.tf reads `trusted_proxies static (...)` with this regex; run the same one here."""
    main = (TERRAFORM / "main.tf").read_text()
    expect('"trusted_proxies static ([^\\n]+)"' in main, "main.tf regex changed")
    expect('file("${path.module}/../compose/Caddyfile")' in main, "main.tf must read the Caddyfile")
    match = re.search(r"trusted_proxies static ([^\n]+)", CADDYFILE.read_text())
    expect(match is not None, "the Caddyfile lost its trusted_proxies line")
    ranges = match.group(1).strip().split(" ") if match else []
    from infra.test_caddyfile import CLOUDFLARE_RANGES

    expect(ranges == list(CLOUDFLARE_RANGES), ranges)
    for cidr in ranges:
        network = ipaddress.ip_network(cidr)
        expect(not network.is_private and network.prefixlen >= 8, f"{cidr} is not an edge range")
    # Both regexes take the first match: a comment quoting the directive would be read instead.
    expect(CADDYFILE.read_text().count("trusted_proxies static") == 1, "one trusted_proxies line, no copies")
