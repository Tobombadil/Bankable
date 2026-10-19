"""infra/ci/beta.py and infra/ci/beta.sh, the steps of .github/workflows/deploy-beta.yml, offline: the
decisions as functions, and the shell steps against PATH shims that record every ssh/scp/curl/tofu/
aws call (no network, no cloud account, no real key). The env file beta.py renders is fed to the
real deploy.sh, so the two cannot drift apart. Checks use `expect` instead of bare `assert` (ruff S101)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from infra.ci import beta
from infra.test_scripts import REQUIRED_FAKES, SSH_SHIM

ROOT = pathlib.Path(__file__).resolve().parents[2]
BETA_SH = ROOT / "infra" / "ci" / "beta.sh"
BASH = shutil.which("bash") or "/bin/bash"
DOMAIN = "example.org"  # any domain: the workflow reads it from the DOMAIN variable only


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


def fake_hashpw(password: bytes, salt: bytes) -> bytes:
    """bcrypt's shape without bcrypt (the suite does not install it): `$2b$14$<22-char salt><31>`."""
    return salt + hashlib.sha256(password + salt).hexdigest()[:31].encode()


#: Every input, well formed. Fakes only: none is a real token.
GOOD_ENV = {
    "HCLOUD_TOKEN": "fake-hcloud-token",
    "CLOUDFLARE_API_TOKEN": "fake-cloudflare-token",
    "R2_ACCESS_KEY_ID": "fake-r2-key-id",
    "R2_SECRET_ACCESS_KEY": "fake-r2-secret",
    "SITE_ACCESS_PASSWORD": "correct horse battery",
    "DOMAIN": DOMAIN,
    "CLOUDFLARE_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
    "CLOUDFLARE_ZONE_ID": "fedcba9876543210fedcba9876543210",
    "SITE_ACCESS_USER": "beta",
    "SENDER_POSTAL_ADDRESS": "1 Example Street, Suite #2, Springfield, IL 62701",
}
COMMON = f'"""doc"""\nfrom __future__ import annotations\n\nDOMAIN = "{DOMAIN}"\nAPI_HOST = f"https://api.{{DOMAIN}}"\n'
EDITORIAL = 'DEFAULT_PRODUCT_NAME = "Example Product"\n'


def check(env: dict[str, str], *, tag: str = "sha-abc1234", commit: str = "abc1234def") -> list[beta.Row]:
    rows, _ = beta.check_inputs(env, tag=tag, commit=commit, code_domain=DOMAIN, code_product_name="Example")
    return [r for r in rows if r.problem]


# ------------------------------------------------------------------------------ check-inputs
def test_complete_inputs_pass_and_resolve_the_product_name_from_the_code() -> None:
    rows, resolved = beta.check_inputs(
        GOOD_ENV, tag="sha-abc1234", commit="abc1234def", code_domain=DOMAIN, code_product_name="Example"
    )
    expect([r for r in rows if r.problem] == [], rows)
    expect(resolved == {"tag": "sha-abc1234", "product_name": "Example"}, resolved)
    expect(
        {r.name for r in rows} >= set(beta.SECRETS) | set(beta.VARIABLES) | set(beta.OPTIONAL_VARIABLES), rows
    )


def test_every_missing_input_is_listed_at_once() -> None:
    problems = check({})
    missing = {r.name for r in problems if r.status == "missing"}
    expect(missing == set(beta.SECRETS) | set(beta.VARIABLES), missing)
    markdown = beta.inputs_markdown(
        beta.check_inputs({}, tag="x", commit="", code_domain=None, code_product_name=None)[0]
    )
    for name in (*beta.SECRETS, *beta.VARIABLES):
        expect(f"`{name}`" in markdown, f"{name} not in the summary")


@pytest.mark.parametrize(
    ("change", "name", "fragment"),
    [
        ({"DOMAIN": "other.example"}, "DOMAIN", "differs from services.api.common.DOMAIN"),
        ({"DOMAIN": "Example.org"}, "DOMAIN", "lowercase domain"),
        ({"DOMAIN": "https://example.org"}, "DOMAIN", "lowercase domain"),
        ({"CLOUDFLARE_ZONE_ID": "not-an-id"}, "CLOUDFLARE_ZONE_ID", "32-character"),
        ({"SITE_ACCESS_USER": "beta tester"}, "SITE_ACCESS_USER", "Caddy"),
        ({"SITE_ACCESS_PASSWORD": "Tiny9pw"}, "SITE_ACCESS_PASSWORD", "12 characters"),
        ({"SITE_ACCESS_PASSWORD": "x" * 73}, "SITE_ACCESS_PASSWORD", "72 bytes"),
        ({"HCLOUD_TOKEN": "token\n"}, "HCLOUD_TOKEN", "whitespace"),
        (
            {"SENDER_POSTAL_ADDRESS": "1 Replace Me St, City, ST 00000"},
            "SENDER_POSTAL_ADDRESS",
            "placeholder",
        ),
        ({"SENDER_POSTAL_ADDRESS": "1 O'Hare Rd\n$HOME"}, "SENDER_POSTAL_ADDRESS", "line break"),
        ({"PRODUCT_CONTACT_EMAIL": "hello"}, "PRODUCT_CONTACT_EMAIL", "email"),
    ],
)
def test_malformed_inputs_are_named_without_their_secret_values(
    change: dict[str, str], name: str, fragment: str
) -> None:
    problems = check(GOOD_ENV | change)
    expect([r.name for r in problems] == [name], problems)
    expect(fragment in (problems[0].problem or ""), problems[0])
    if name in beta.SECRETS:
        expect(change[name] not in (problems[0].problem or ""), "a secret's value must never be echoed")


def test_the_tag_must_name_a_commit_whose_domain_is_read() -> None:
    expect([r.name for r in check(GOOD_ENV, tag="latest")] == ["tag"], "latest is not a deployable tag")
    expect([r.name for r in check(GOOD_ENV, commit="")] == ["tag"], "a tag with no commit is refused")
    rows, _ = beta.check_inputs(
        GOOD_ENV, tag="sha-abc1234", commit="c", code_domain=None, code_product_name="x"
    )
    expect([r.name for r in rows if r.problem] == ["DOMAIN"], "an unreadable code DOMAIN fails")


def test_the_code_constants_are_read_without_importing_the_code() -> None:
    expect(beta.code_constant(COMMON, "DOMAIN") == DOMAIN, "DOMAIN")
    expect(beta.code_constant(COMMON, "API_HOST") is None, "an f-string is not a literal")
    expect(beta.code_constant("DOMAIN: str = 'a.example'\n", "DOMAIN") == "a.example", "annotated")
    expect(beta.code_constant("def broken(:\n", "DOMAIN") is None, "a syntax error reads as absent")
    real = (ROOT / "services" / "api" / "common.py").read_text()
    expect(
        beta.code_constant(real, "DOMAIN") is not None, "services/api/common.py DOMAIN must stay a literal"
    )
    editorial = (ROOT / "services" / "social" / "editorial.py").read_text()
    expect(
        bool(beta.code_constant(editorial, "DEFAULT_PRODUCT_NAME")),
        "DEFAULT_PRODUCT_NAME must stay a literal",
    )


# ------------------------------------------------------------------------------ OpenTofu decisions
def test_tfvars_are_the_single_host_beta_with_dns_from_the_domain_variable() -> None:
    tfvars = beta.tfvars(GOOD_ENV, server_type="cpx31", bucket_prefix="beta-object-storage")
    expect(tfvars["domain"] == DOMAIN and tfvars["environment"] == "production", tfvars)
    expect(tfvars["single_host"] is True and tfvars["generate_secrets"] is True, tfvars)
    expect(tfvars["dns_proxied"] is False, "records point straight at the VM for now (docs/64 §2)")
    expect(
        not any(k in json.dumps(tfvars) for k in ("fake-r2-secret", "fake-hcloud-token")),
        "no secret is a variable",
    )
    variables = (ROOT / "infra" / "terraform" / "variables.tf").read_text()
    variables += (ROOT / "infra" / "terraform" / "generated.tf").read_text()
    for name in tfvars:
        expect(f'variable "{name}"' in variables, f"{name} is not an OpenTofu variable")


def _server_types() -> dict[str, Any]:
    """`GET /server_types` as Hetzner answers it since `GET /datacenters` went (1 October 2026):
    availability and deprecation per location, under `locations`."""

    def st(i: int, name: str, cores: int, memory: float, price: str, **extra: Any) -> dict[str, Any]:
        here = {
            "id": 4,
            "name": "ash",
            "deprecation": extra.pop("deprecation", None),
            "recommended": False,
            "available": extra.pop("available", True),
        }
        return {
            "id": i,
            "name": name,
            "cores": cores,
            "memory": memory,
            "cpu_type": extra.pop("cpu_type", "shared"),
            "architecture": extra.pop("architecture", "x86"),
            "deprecation": None,
            "prices": [{"location": "ash", "price_monthly": {"net": price, "gross": price}}],
            "locations": [here],
            **extra,
        }

    types = {
        "server_types": [
            st(1, "cx32", 4, 8.0, "7.0", deprecation={"announced": "2025-10-16"}),
            st(2, "cpx21", 3, 4.0, "8.0"),
            st(3, "cpx31", 4, 8.0, "16.0"),
            st(4, "cpx32", 4, 8.0, "14.0", available=False),
            st(5, "cax21", 4, 8.0, "6.0", architecture="arm"),
            st(6, "ccx23", 4, 16.0, "30.0", cpu_type="dedicated"),
            st(7, "cpx41", 8, 16.0, "30.0"),
        ]
    }
    return types


def test_the_server_type_is_the_cheapest_available_x86_with_4_vcpu_and_8_gb() -> None:
    types = _server_types()
    name, why = beta.pick_server_type(types, location="ash")
    expect(name == "cpx31", f"{name}: cx32 is deprecated, cpx32 unavailable here, cax21 is arm ({why})")
    for st in types["server_types"]:
        st["locations"][0]["available"] = st["name"] == "ccx23"
    expect(beta.pick_server_type(types, location="ash")[0] == "ccx23", "dedicated only as a fallback")
    with pytest.raises(beta.InputError):
        beta.pick_server_type(types, location="fsn1")


def test_a_deprecated_type_is_orderable_until_its_date_and_a_refusal_lists_every_reason() -> None:
    now = datetime(2026, 10, 19, tzinfo=UTC)
    types = _server_types()
    for st in types["server_types"]:
        here = st["locations"][0]
        here["deprecation"] = {
            "announced": "2026-06-01T00:00:00+00:00",
            "unavailable_after": "2027-01-01T00:00:00+00:00",
        }
        here["available"] = st["name"] in ("cpx31", "ccx23")
    name, why = beta.pick_server_type(types, location="ash", now=now)
    expect(name == "cpx31" and "deprecated" in why, f"orderable until 2027, shared before dedicated: {why}")
    for st in types["server_types"]:
        st["locations"][0]["deprecation"]["unavailable_after"] = "2026-10-01T00:00:00+00:00"
    with pytest.raises(beta.InputError) as refused:
        beta.pick_server_type(types, location="ash", now=now)
    message = str(refused.value)
    expect("cpx31: retired in ash" in message and "cpx32: out of stock in ash" in message, message)


def _plan(*changes: tuple[str, str, list[str]]) -> dict[str, Any]:
    return {
        "resource_changes": [
            {"address": address, "type": rtype, "change": {"actions": actions}}
            for address, rtype, actions in changes
        ]
    }


def test_the_plan_guard_refuses_any_deletion_but_a_dns_record() -> None:
    fine = _plan(
        ("hcloud_server.app", "hcloud_server", ["create"]),
        ("cloudflare_record.app[0]", "cloudflare_record", ["delete", "create"]),
        ("hcloud_firewall.web", "hcloud_firewall", ["update"]),
        ('random_password.generated["SESSION_SECRET"]', "random_password", ["no-op"]),
    )
    refused, counts = beta.plan_guard(fine)
    expect(refused == [] and counts["replace"] == 1, (refused, counts))
    for address, rtype in (
        ("hcloud_server.app", "hcloud_server"),
        ("hcloud_ssh_key.operator", "hcloud_ssh_key"),
        ("cloudflare_r2_bucket.object_storage[0]", "cloudflare_r2_bucket"),
        ('random_password.generated["POSTGRES_PASSWORD"]', "random_password"),
        ("tls_private_key.deploy[0]", "tls_private_key"),
    ):
        for actions in (["delete", "create"], ["create", "delete"], ["delete"]):
            refused, _ = beta.plan_guard(_plan((address, rtype, actions)))
            expect(bool(refused) and address in refused[0], f"{address} {actions} must be refused")


# ------------------------------------------------------------------------------ outputs and masking
def outputs(**extra: Any) -> dict[str, Any]:
    generated = {
        k: f"generated-{k.lower()}-0123456789abcdefABCDEF" for k in (*beta.GENERATED_KEYS, "SITE_ACCESS_SALT")
    }
    base: dict[str, Any] = {
        "app_ipv4": {"sensitive": False, "value": "203.0.113.10"},
        "app_server_type": {"sensitive": False, "value": "cpx31"},
        "generated_secrets": {"sensitive": True, "value": generated},
        "deploy_ssh_private_key": {
            "sensitive": True,
            "value": "-----BEGIN OPENSSH "
            "PRIVATE KEY-----\nfakekeylineone\nfakekeylinetwo\n"  # split so the secret scan skips this fake
            "-----END OPENSSH PRIVATE KEY-----\n",
        },
        "tile_url": {"sensitive": False, "value": f"https://tiles.{DOMAIN}/basemap.pmtiles"},
        "r2_bucket_name": {"sensitive": False, "value": "beta-object-storage-production"},
        "tiles_bucket_name": {"sensitive": False, "value": "beta-object-storage-tiles-production"},
    }
    return base | extra


def test_every_sensitive_value_and_each_key_line_is_masked_and_nothing_else() -> None:
    commands = beta.mask_commands(beta.sensitive_strings(outputs()))
    masked = {c.removeprefix("::add-mask::") for c in commands}
    for value in outputs()["generated_secrets"]["value"].values():
        expect(value in masked, value)
    expect({"fakekeylineone", "fakekeylinetwo"} <= masked, "each line of the key")
    expect("203.0.113.10" not in masked and "cpx31" not in masked, "plain outputs are not masked")
    expect(all(c.startswith("::add-mask::") for c in commands), commands)


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("plain-value_1.2:3/4@5+6=7,8%9", "K=plain-value_1.2:3/4@5+6=7,8%9"),
        ("1 Main St, Suite #2", "K='1 Main St, Suite #2'"),
        ("1 O'Hare Rd", 'K="1 O\'Hare Rd"'),
        ("", "K="),
    ],
)
def test_env_lines_are_bare_single_or_double_quoted(value: str, line: str) -> None:
    expect(beta.dotenv_line("K", value) == line, beta.dotenv_line("K", value))


@pytest.mark.parametrize("value", ["a\nb", "O'Hare $HOME", "O'Hare \\n", 'O\'Hare "x"'])
def test_env_values_no_reader_would_read_back_are_refused(value: str) -> None:
    with pytest.raises(beta.InputError):
        beta.dotenv_line("K", value)


def test_the_gate_hash_is_base64_bcrypt_and_stable_for_one_password() -> None:
    salt = beta.bcrypt_salt("seed")
    expect(len(salt) == 22 and set(salt) <= set(beta.BCRYPT_ALPHABET), salt)
    expect(salt[-1] in ".Oeu", "the last character carries 2 bits, the rest zero (bcrypt refuses others)")
    first = beta.site_access_hash("correct horse battery", "seed", fake_hashpw)
    expect(
        first == beta.site_access_hash("correct horse battery", "seed", fake_hashpw),
        "same password, same hash",
    )
    expect(first != beta.site_access_hash("another password!", "seed", fake_hashpw), "new password, new hash")
    raw = base64.b64decode(first).decode()
    expect(raw.startswith("$2b$14$"), raw)
    with pytest.raises(beta.InputError):
        beta.site_access_hash("x" * 73, "seed", fake_hashpw)


def test_the_gate_hash_verifies_with_real_bcrypt() -> None:
    bcrypt = pytest.importorskip(
        "bcrypt", reason="bcrypt is installed on the runner (infra/ci/requirements-deploy.txt), not here"
    )
    hashed = beta.site_access_hash("correct horse battery", "seed", bcrypt.hashpw)
    expect(bcrypt.checkpw(b"correct horse battery", base64.b64decode(hashed)), "checkpw")


def test_the_env_file_derives_everything_from_the_domain_and_the_state() -> None:
    env = GOOD_ENV | {"PRODUCT_NAME": "Example Product"}
    pairs = dict(beta.render_env(env, outputs(), site_hash="aGFzaA=="))
    expect(pairs["DOMAIN"] == DOMAIN and pairs["PRODUCT_URL"] == f"https://{DOMAIN}", pairs)
    expect(pairs["PRODUCT_CONTACT_EMAIL"] == f"hello@{DOMAIN}", "default contact on the domain")
    expect(
        pairs["SENDER_LEGAL_NAME"] == beta.DEFAULT_SENDER_LEGAL_NAME, "the decided legal sender by default"
    )
    expect(
        pairs["MAP_TILE_URL"] == f"https://tiles.{DOMAIN}/basemap.pmtiles",
        "a PMTiles URL, not a {z} template",
    )
    beta_values = {
        "SNAPSHOT_STORE": "local",
        "SITE_ACCESS": "basic",
        "CSP_MODE": "report",
        "PLATFORM_POSTURE": "noncommercial",
    }
    expect(all(pairs[k] == v for k, v in beta_values.items()), "docs/64's beta values")
    password = outputs()["generated_secrets"]["value"]["POSTGRES_PASSWORD"]
    expect(
        pairs["DATABASE_URL"] == f"postgresql+psycopg://infraque:{password}@postgres:5432/infraque",
        pairs["DATABASE_URL"],
    )
    expect(
        "ENVIRONMENT" not in pairs and "RESEND_API_KEY" not in pairs and "SENTRY_DSN" not in pairs,
        "v1 leaves them out",
    )
    expect(set(REQUIRED_FAKES) | {"API_INTERNAL_TOKEN"} <= set(pairs), "every key deploy.sh requires")
    custom = dict(
        beta.render_env(env | {"PRODUCT_CONTACT_EMAIL": "ops@other.example"}, outputs(), site_hash="x")
    )
    expect(custom["PRODUCT_CONTACT_EMAIL"] == "ops@other.example", custom)
    with pytest.raises(beta.InputError):
        beta.render_env(env, outputs(generated_secrets={"sensitive": True, "value": {}}), site_hash="x")


def test_no_domain_or_product_name_is_hard_coded() -> None:
    """The domain comes from the DOMAIN variable and the product name from the code (coordinator,
    2026-10-15): neither may be written into the workflow, its scripts or its backend."""
    files = [ROOT / ".github" / "workflows" / "deploy-beta.yml", BETA_SH, ROOT / "infra" / "ci" / "beta.py"]
    files += [ROOT / "infra" / "ci" / "seed_on_host.sh", ROOT / "infra" / "ci" / "backend_r2.tf"]
    files += [ROOT / "infra" / "terraform" / "generated.tf"]
    for path in files:
        text = path.read_text()
        for name in (
            "infraque.com",
            "inframation.io",
            "gridpipeline",
            "grid pipeline",
            "tiles.infra",
            "hello@infra",
        ):
            expect(name not in text.lower(), f"{path.name} names {name!r}")
        for brand in ("Infraque", "Inframation", "Grid Pipeline"):
            expect(brand not in text, f"{path.name} names the product")


# ------------------------------------------------------------------------------ preflight summary
def _report(verdict: str) -> dict[str, Any]:
    return {
        "preflight": 1,
        "verdict": verdict,
        "exit_code": 0 if verdict == "GO" else 1,
        "duration_s": 12.5,
        "totals": {"PASS": 10, "WARN": 1, "FAIL": 0 if verdict == "GO" else 1},
        "checks": [
            {
                "n": 9,
                "id": "email",
                "title": "email",
                "status": "FAIL",
                "summary": "no RESEND_API_KEY",
                "actions": ["set it | later"],
            },
            {
                "n": 1,
                "id": "database",
                "title": "database",
                "status": "PASS",
                "summary": "at head",
                "actions": [],
            },
        ],
        "record": "/var/lib/infraque/data/preflight/x.json",
    }


def test_a_no_go_report_is_a_table_and_a_green_step(tmp_path: pathlib.Path) -> None:
    stdout = tmp_path / "out"
    stdout.write_text("Container starting\n" + json.dumps(_report("NO-GO"), indent=1) + "\n")
    summary = tmp_path / "summary.md"
    os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
    try:
        expect(
            beta.main(["preflight-summary", str(stdout), "--exit-code", "1"]) == 0, "NO-GO is not a failure"
        )
    finally:
        del os.environ["GITHUB_STEP_SUMMARY"]
    text = summary.read_text()
    expect("### Preflight: NO-GO" in text and "| 9 | FAIL | email |" in text, text)
    expect("set it \\| later" in text, "pipes in cells are escaped")


@pytest.mark.parametrize(
    ("body", "code"), [("Traceback (most recent call last):\n", 1), ("", 255), ("{report}", 137)]
)
def test_a_preflight_that_could_not_run_fails_the_step(tmp_path: pathlib.Path, body: str, code: int) -> None:
    stdout = tmp_path / "out"
    stdout.write_text(body.replace("{report}", json.dumps(_report("GO"))))
    expect(beta.main(["preflight-summary", str(stdout), "--exit-code", str(code)]) == 1, body)


# ------------------------------------------------------------------------------ beta.sh, against shims
CURL_SHIM = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$FAKE_LOG"
out=""; prev=""
for a in "$@"; do [[ "$prev" == "-o" ]] && out="$a"; prev="$a"; done
case "$*" in
  *"/v1/health"*) body='{"status":"ok","checks":{"database":true,"queue":true}}' ;;
  *"/health"*) body='ok' ;;
  *"-X GET"*"/domains/custom"*) body='{"success":true,"result":{"domains":[]}}' ;;
  *"/r2/buckets?per_page=1"*) body='{"success":true,"result":{}}'
    [[ -z "${FAKE_CF_R2_DENIED:-}" ]] \
      || body='{"success":false,"errors":[{"code":10000,"message":"Authentication error"}]}' ;;
  *"api.cloudflare.com"*) body='{"success":true,"result":{}}' ;;
  *"api.hetzner.cloud/v1/server_types"*) body="${FAKE_HCLOUD_SERVER_TYPES:-}" ;;
  *"api.github.com"*"/actions/workflows/release.yml/runs"*) body="${FAKE_RELEASE_RUNS:-}" ;;
  *) body="${FAKE_DOWNLOAD:-}" ;;
esac
if [[ -n "$out" ]]; then printf '%s' "$body" > "$out"; else printf '%s' "$body"; fi
case "$*" in
  *"-w %{http_code}"*"https://${FAKE_DOMAIN:-none}/"|*"--resolve"*"-w %{http_code}"*) printf '401' ;;
  *"-w %{http_code}"*) printf '200' ;;
esac
exit 0
"""
LOGGING = '#!/usr/bin/env bash\nprintf \'{name} %s\\n\' "$*" >> "$FAKE_LOG"\n{body}exit {rc}\n'


@pytest.fixture()
def sh(tmp_path: pathlib.Path) -> Iterator[tuple[pathlib.Path, dict[str, str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def shim(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    shim("ssh", SSH_SHIM)
    shim("scp", LOGGING.format(name="scp", body="", rc=0))
    shim("curl", CURL_SHIM)
    shim("aws", LOGGING.format(name="aws", body="", rc="${FAKE_AWS_RC:-0}"))
    shim("docker", LOGGING.format(name="docker", body="", rc="${FAKE_DOCKER_RC:-0}"))
    shim(
        "tofu",
        LOGGING.format(
            name="tofu", body='[[ "$*" == *"output -raw"* ]] && echo "${FAKE_TOFU_OUTPUT:-}"\n', rc=0
        ),
    )
    log = tmp_path / "calls.log"
    log.touch()
    runner = tmp_path / "runner"
    runner.mkdir()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
        "RUNNER_TEMP": str(runner),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_ENV": str(tmp_path / "github_env"),
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        "GITHUB_PATH": str(tmp_path / "github_path"),
        "APP_HOST": "203.0.113.10",
        "DOMAIN": DOMAIN,
        "FAKE_DOMAIN": DOMAIN,
        "URL_INTERVAL_SECONDS": "0",
        "HOST_WAIT_INTERVAL": "0",
    }
    yield log, env


def beta_sh(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed script path under the repo, no shell
        [BASH, str(BETA_SH), *args], capture_output=True, text=True, cwd=ROOT, env=env, check=False
    )


def lines_of(log: pathlib.Path) -> list[str]:
    return log.read_text().splitlines()


def test_a_download_whose_checksum_differs_is_never_unpacked(sh: tuple[pathlib.Path, dict[str, str]]) -> None:
    log, env = sh
    result = beta_sh("install-tofu", env=env | {"FAKE_DOWNLOAD": "not the release"})
    expect(result.returncode != 0 and "checksum mismatch" in result.stdout, result.stdout + result.stderr)
    expect(
        not (pathlib.Path(env["RUNNER_TEMP"]) / "deploy-beta" / "bin" / "tofu").exists(), "nothing installed"
    )
    expect("v1.12.7/tofu_1.12.7_linux_amd64.tar.gz" in lines_of(log)[0], lines_of(log))


def test_check_inputs_stops_red_listing_everything_missing_without_a_cloud_call(
    sh: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = sh
    result = beta_sh("check-inputs", env=env | {"GITHUB_SHA": "0" * 40, "PYTHON": "python3", "DOMAIN": ""})
    expect(result.returncode == 1, result.stdout + result.stderr)
    summary = pathlib.Path(env["GITHUB_STEP_SUMMARY"]).read_text()
    for name in (*beta.SECRETS, *beta.VARIABLES):
        expect(f"| `{name}` |" in summary and f"::error::{name}:" in result.stdout, name)
    expect(lines_of(log) == [], f"no network or host call: {lines_of(log)}")


def test_check_inputs_reads_the_domain_from_the_tags_commit(sh: tuple[pathlib.Path, dict[str, str]]) -> None:
    log, env = sh
    git = shutil.which("git") or "git"
    head = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [git, "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.strip()
    code_domain = beta.code_constant((ROOT / "services" / "api" / "common.py").read_text(), "DOMAIN") or ""
    good = env | GOOD_ENV | {"DOMAIN": code_domain, "INPUT_TAG": f"sha-{head[:7]}", "PYTHON": "python3"}
    result = beta_sh("check-inputs", env=good)
    expect(result.returncode == 0, result.stdout + result.stderr)
    output = pathlib.Path(env["GITHUB_OUTPUT"]).read_text()
    expect(f"tag=sha-{head[:7]}" in output and "product_name=" in output, output)
    wrong = beta_sh("check-inputs", env=good | {"DOMAIN": "not-the-code.example"})
    expect(wrong.returncode == 1 and "differs from services.api.common.DOMAIN" in wrong.stdout, wrong.stdout)
    expect(lines_of(log) == [], "no network call")


# OpenTofu 1.12's `tofu output` on a state without outputs: this warning on stdout, exit 0.
NO_OUTPUTS_WARNING = "\x1b[33m╷\x1b[0m\n\x1b[33m│\x1b[0m Warning: No outputs found\n\x1b[33m╵\x1b[0m"


def _state_with_server(server_type: str) -> str:
    return json.dumps(
        {
            "format_version": "1.0",
            "values": {
                "root_module": {
                    "resources": [
                        {"address": "hcloud_network.app", "values": {"name": "net"}},
                        {"address": "hcloud_server.app", "values": {"server_type": server_type}},
                    ]
                }
            },
        }
    )


def _tofu_shim(env: dict[str, str], *, state: str, plan: str) -> None:
    tofu = pathlib.Path(env["PATH"].split(":")[0]) / "tofu"
    tofu.write_text(
        '#!/usr/bin/env bash\nprintf \'tofu %s\\n\' "$*" >> "$FAKE_LOG"\n'
        f'[[ "$*" == *"output"* ]] && printf \'%s\\n\' {json.dumps(NO_OUTPUTS_WARNING)}\n'
        f'[[ "$*" == *"show -json"*"plan.bin"* ]] && {{ echo \'{plan}\'; exit 0; }}\n'
        f'[[ "$*" == *"show -json"* ]] && echo \'{state}\'\nexit 0\n'
    )


def test_an_empty_tag_resolves_to_the_newest_build_in_the_runs_history(
    sh: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = sh
    git = shutil.which("git") or "git"
    head = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [git, "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.strip()
    code_domain = beta.code_constant((ROOT / "services" / "api" / "common.py").read_text(), "DOMAIN") or ""
    # release-images' successful runs, newest first: one for a commit outside this history, then HEAD's.
    runs = {"workflow_runs": [{"head_sha": "e" * 40}, {"head_sha": head}]}
    good = (
        env
        | GOOD_ENV
        | {
            "DOMAIN": code_domain,
            "PYTHON": "python3",
            "GITHUB_SHA": "f" * 40,  # a merge whose images do not exist yet
            "GITHUB_TOKEN": "fake-github-token",
            "GITHUB_REPOSITORY": "example/repo",
            "FAKE_RELEASE_RUNS": json.dumps(runs),
        }
    )
    result = beta_sh("check-inputs", env=good)
    expect(result.returncode == 0, result.stdout + result.stderr)
    output = pathlib.Path(env["GITHUB_OUTPUT"]).read_text()
    expect(f"tag=sha-{head[:7]}" in output, output)
    expect(f"deploying sha-{head[:7]}" in result.stderr, result.stderr)
    calls = lines_of(log)
    expect(len(calls) == 1 and "/actions/workflows/release.yml/runs?" in calls[0], calls)


def test_tofu_apply_reuses_the_states_server_type_and_never_applies_a_refused_plan(
    sh: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    log, env = sh
    env = env | GOOD_ENV
    # `tofu show -json` prints the state (a cpx31 server) or, given the plan file, the plan the guard reads:
    # a replaced server.
    plan = json.dumps(_plan(("hcloud_server.app", "hcloud_server", ["delete", "create"])))
    _tofu_shim(env, state=_state_with_server("cpx31"), plan=plan)
    result = beta_sh("tofu-apply", env=env)
    calls = lines_of(log)
    expect(result.returncode == 1 and "hcloud_server.app" in result.stdout, result.stdout + result.stderr)
    expect(not any(" apply " in ln for ln in calls), f"apply must not run: {calls}")
    expect(not any("api.hetzner.cloud" in ln for ln in calls), "the state's type is reused, no lookup")
    plan_line = next(ln for ln in calls if " plan " in ln)
    tfvars = json.loads((pathlib.Path(env["RUNNER_TEMP"]) / "deploy-beta" / "beta.tfvars.json").read_text())
    expect(tfvars["app_server_type"] == "cpx31" and "-var-file=" in plan_line, (tfvars, plan_line))
    expect(not (pathlib.Path(env["RUNNER_TEMP"]) / "deploy-beta" / "plan.json").exists(), "plan.json removed")


def test_a_first_apply_picks_the_type_from_hetzner_not_the_empty_states_warning(
    sh: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = sh
    env = env | GOOD_ENV | {"FAKE_HCLOUD_SERVER_TYPES": json.dumps(_server_types())}
    _tofu_shim(env, state='{"format_version":"1.0"}', plan=json.dumps(_plan()))
    result = beta_sh("tofu-apply", env=env)
    calls = lines_of(log)
    expect(result.returncode == 0, result.stdout + result.stderr)
    tfvars = json.loads((pathlib.Path(env["RUNNER_TEMP"]) / "deploy-beta" / "beta.tfvars.json").read_text())
    picked = tfvars["app_server_type"]
    expect(picked == "cpx31", f"Hetzner's pick, not the warning: {picked!r}")
    expect(any("api.hetzner.cloud/v1/server_types" in ln for ln in calls), calls)
    expect(not any("/v1/datacenters" in ln for ln in calls), f"GET /datacenters answers 410 Gone: {calls}")
    expect(any(" apply " in ln for ln in calls), f"the plan is applied: {calls}")


def test_a_cloudflare_token_without_r2_stops_before_anything_is_planned(
    sh: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = sh
    env = env | GOOD_ENV | {"FAKE_CF_R2_DENIED": "1"}
    _tofu_shim(env, state='{"format_version":"1.0"}', plan=json.dumps(_plan()))
    result = beta_sh("tofu-apply", env=env)
    calls = lines_of(log)
    expect(result.returncode == 1, result.stdout + result.stderr)
    expect("Workers R2 Storage" in result.stdout, result.stdout)
    expect("10000: Authentication error" in result.stdout, "Cloudflare's own error is shown")
    expect(not any(ln.startswith("tofu ") for ln in calls), f"no plan, no apply: {calls}")


def test_status_only_reads(sh: tuple[pathlib.Path, dict[str, str]]) -> None:
    _log, env = sh
    result = beta_sh("status", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    script = BETA_SH.read_text().split("<<'REMOTE'", 1)[1].split("\nREMOTE\n", 1)[0]
    for verb in (
        " up ",
        " stop ",
        " run ",
        " rm ",
        " down",
        "restart",
        "systemctl start",
        "insert ",
        "update ",
        "delete ",
    ):
        expect(verb not in script, f"status runs {verb.strip()!r}")
    expect(
        "== https://api." + DOMAIN + "/v1/health" in pathlib.Path(env["GITHUB_STEP_SUMMARY"]).read_text(),
        "summary",
    )


def test_seed_queues_the_fetches_then_starts_the_waiter_and_returns(
    sh: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = sh
    result = beta_sh("seed", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    calls = lines_of(log)
    fetch = next(i for i, ln in enumerate(calls) if "bootstrap fetch" in ln)
    ship = next(i for i, ln in enumerate(calls) if ln.startswith("scp ") and "seed_on_host.sh" in ln)
    unit = next(i for i, ln in enumerate(calls) if "systemd-run" in ln and "infraque-seed-context" in ln)
    expect(fetch < ship < unit, calls)
    expect(
        "compose.single.yml" in calls[fetch] and "IMAGE_TAG=$(cat /opt/infraque/current-tag)" in calls[fetch],
        calls[fetch],
    )
    expect(
        not any("bootstrap context" in ln for ln in calls), "the context build is the host's to queue, later"
    )
    waiter = (ROOT / "infra" / "ci" / "seed_on_host.sh").read_text()
    expect(
        waiter.index("pending_sql=")
        < waiter.index("worker python -m infra.scheduler.bootstrap context --build"),
        "it waits before it queues",
    )
    expect("'run_connector', 'load_source'" in waiter, "it waits for the fetches and their loads")


def test_preflight_runs_the_documented_command_and_tolerates_no_go(
    sh: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = sh
    report = json.dumps(_report("NO-GO"))
    ssh = pathlib.Path(env["PATH"].split(":")[0]) / "ssh"
    ssh.write_text(
        '#!/usr/bin/env bash\nprintf \'ssh %s\\n\' "$*" >> "$FAKE_LOG"\n'
        f'[[ "$*" == *"preflight --json"* ]] && {{ echo \'{report}\'; exit 1; }}\nexit 0\n'
    )
    result = beta_sh("preflight", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    call = next(ln for ln in lines_of(log) if "preflight --json" in ln)
    expect(
        "run --rm --no-deps -T" in call
        and "worker python -m infra.scheduler.bootstrap preflight --json" in call,
        call,
    )
    expect("### Preflight: NO-GO" in pathlib.Path(env["GITHUB_STEP_SUMMARY"]).read_text(), "summary table")


def test_health_pins_the_server_first_then_checks_the_gate_then_public_dns(
    sh: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = sh
    result = beta_sh("health", env=env | {"TAG": "sha-abc1234"})
    expect(result.returncode == 0, result.stdout + result.stderr)
    calls = [ln for ln in lines_of(log) if ln.startswith("curl ")]
    pinned = [
        i
        for i, ln in enumerate(calls)
        if f"--resolve {DOMAIN}:443:203.0.113.10" in ln or "--resolve api." in ln
    ]
    public = [i for i, ln in enumerate(calls) if "--resolve" not in ln]
    expect(bool(pinned) and bool(public) and max(pinned) < min(public), calls)
    expect(
        any(f"https://{DOMAIN}/health" in ln for ln in calls)
        and any(f"https://api.{DOMAIN}/v1/health" in ln for ln in calls),
        calls,
    )
    summary = pathlib.Path(env["GITHUB_STEP_SUMMARY"]).read_text()
    expect(
        f"https://admin.{DOMAIN}/admin" in summary and f"https://tiles.{DOMAIN}/basemap.pmtiles" in summary,
        summary,
    )


def test_the_rendered_env_file_passes_every_deploy_sh_check(tmp_path: pathlib.Path) -> None:
    """beta.py's env file through the real deploy.sh (SINGLE_HOST=1) against the ssh shim: the gate
    hash, DATABASE_URL, quoting and every required key are accepted, and nothing needs sops."""
    pairs = beta.render_env(
        GOOD_ENV | {"PRODUCT_NAME": "Example Product"},
        outputs(),
        site_hash=beta.site_access_hash("correct horse battery", "seed", fake_hashpw),
    )
    dotenv = tmp_path / "beta.env"
    beta.write_private(dotenv, beta.ENV_HEADER + "".join(beta.dotenv_line(k, v) + "\n" for k, v in pairs))
    expect(stat.S_IMODE(dotenv.stat().st_mode) == 0o600, oct(dotenv.stat().st_mode))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", SSH_SHIM), ("scp", LOGGING.format(name="scp", body="", rc=0))):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "sops").write_text("#!/usr/bin/env bash\nexit 97\n")
    (bin_dir / "sops").chmod(0o755)
    log = tmp_path / "calls.log"
    shipped = tmp_path / "shipped.env"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
        "FAKE_SECRETS": str(shipped),
        "APP_HOST": "203.0.113.10",
        "SINGLE_HOST": "1",
        "DEPLOY_ENV_FILE": str(dotenv),
        "DEPLOY_LOG": str(tmp_path / "deploy-log.md"),
        "HEALTH_TIMEOUT_SECONDS": "1",
        "HEALTH_INTERVAL_SECONDS": "0",
    }
    result = subprocess.run(  # noqa: S603 -- fixed script path under the repo, no shell
        [BASH, str(ROOT / "infra" / "scripts" / "deploy.sh"), "production", "sha-abc1234"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    expect(result.returncode == 0, result.stdout + result.stderr)
    text = shipped.read_text()
    expect("SENDER_POSTAL_ADDRESS='1 Example Street, Suite #2, Springfield, IL 62701'" in text, text)
    expect(text.rstrip().endswith("ENVIRONMENT=production"), "deploy.sh appends the environment")
