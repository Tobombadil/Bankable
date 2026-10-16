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


# ---------------------------------------------------------------- the GitHub Actions beta deploy (2026-10-15)
def test_generated_secrets_are_off_unless_the_beta_workflow_asks() -> None:
    """generated.tf plans nothing by default, so a local `tofu apply` is unchanged: every resource is
    counted or iterated on `generate_secrets`, whose default is false; its outputs are sensitive."""
    generated = (TERRAFORM / "generated.tf").read_text()
    block = re.search(r'variable "generate_secrets" \{(.*?)\n\}', generated, re.S)
    expect(
        block is not None and "default     = false" in block.group(1), "generate_secrets defaults to false"
    )
    for kind, name in re.findall(r'resource "(\w+)" "(\w+)" \{', generated):
        body = generated.split(f'resource "{kind}" "{name}" {{', 1)[1].split("\n}", 1)[0]
        expect("var.generate_secrets ?" in body, f"{kind}.{name} is not conditional on generate_secrets")
    for name in ("generated_secrets", "deploy_ssh_private_key"):
        body = generated.split(f'output "{name}" {{', 1)[1].split("\n}", 1)[0]
        expect("sensitive   = true" in body, f"{name} must be sensitive")
    expect(
        "special  = false" in generated,
        "letters and digits only: DATABASE_URL carries the password unescaped",
    )
    main = (TERRAFORM / "main.tf").read_text()
    expect(
        "public_key = local.operator_public_key" in main, "the operator key is the generated one when asked"
    )


def test_dns_stays_proxied_by_default() -> None:
    variables = (TERRAFORM / "variables.tf").read_text()
    block = re.search(r'variable "dns_proxied" \{(.*?)\n\}', variables, re.S)
    expect(block is not None and "default     = true" in block.group(1), "dns_proxied defaults to true")
    dns = (TERRAFORM / "dns.tf").read_text()
    expect(dns.count("proxied = var.dns_proxied") == 4 and "proxied = true" not in dns, dns)


def test_cloud_init_installs_no_apt_package_ubuntu_24_04_lacks() -> None:
    """The docker-ce image is Ubuntu 24.04, which has no `awscli` package; one unknown package fails
    the whole apt install, pg_dump included, and every deploy's pre-migration dump with it."""
    import yaml

    app = yaml.safe_load((TERRAFORM / "cloud-init" / "app.yaml").read_text())
    expect("awscli" not in app["packages"] and "postgresql-client-16" in app["packages"], app["packages"])
    expect(
        any("pip install" in c and "awscli==" in c and "/usr/local/bin/aws" in c for c in app["runcmd"]),
        app["runcmd"],
    )
    unit = app["write_files"][0]["content"]
    expect(
        "AWS_DEFAULT_REGION=auto" in unit and "AWS_REQUEST_CHECKSUM_CALCULATION=when_required" in unit, unit
    )
