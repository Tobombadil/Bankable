"""The decisions `.github/workflows/deploy-beta.yml` makes on the runner, as functions with tests
(infra/ci/test_beta.py). The workflow deploys the one-server beta (docs/64 §2, "One-click route")
from the GitHub Environment `production`; infra/ci/beta.sh runs each step and calls these.

Standard library only, so the runner's own `python3` runs it, except `render-env`, which imports
`bcrypt` from the hash-pinned infra/ci/requirements-deploy.txt.

    check-inputs   every secret and variable present and well formed; DOMAIN equal to the tag's
                   `services.api.common.DOMAIN` (the rule of preflight check 10). Calls no cloud API.
    tfvars         the OpenTofu variables of the beta (single host, generated secrets, DNS records
                   for DOMAIN not proxied yet).
    pick-server-type  the cheapest x86 type with 4 vCPU and 8 GB that Hetzner offers in the location.
    plan-guard     refuse a plan that deletes or replaces anything but a DNS record.
    mask / output / write-key   read `tofu output -json`: mask every sensitive value, print one
                   plain output, write the deploy SSH key.
    render-env     the env file deploy.sh ships (DEPLOY_ENV_FILE), 0600.
    preflight-summary  the preflight's JSON report as a Markdown table; fails only when there is no
                   report (the command itself failed), never on NO-GO.

Nothing here prints a secret. Values it generates (the gate's hash) are masked before use.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

#: The owner's inputs (docs/64 §2). Names are fixed: the owner has been given them.
SECRETS = (
    "HCLOUD_TOKEN",
    "CLOUDFLARE_API_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "SITE_ACCESS_PASSWORD",
)
VARIABLES = (
    "DOMAIN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_ZONE_ID",
    "SITE_ACCESS_USER",
    "SENDER_POSTAL_ADDRESS",
)
OPTIONAL_VARIABLES = (
    "SENDER_LEGAL_NAME",
    "PRODUCT_CONTACT_EMAIL",
    "PRODUCT_NAME",
    "SITE_ACCESS",
    "MY_PIPELINE",
    "GRID_LAYERS_REGIONS",
    "PARCELS_COUNTIES",
)
#: The ERCOT Public API account (docs/29 §2.1; owner, 2026-10-22: a person registers it). All three
#: or none: without them the ERCOT price connector works from the public MIS downloads and display
#: pages, and with them it may also use api.ercot.com (pipeline/connectors/prices/ercot_api.py).
OPTIONAL_SECRETS = ("ERCOT_API_SUBSCRIPTION_KEY", "ERCOT_API_USERNAME", "ERCOT_API_PASSWORD")
#: `SITE_ACCESS`: `basic` (the shared login, the default) or `open` (public; owner, 2026-10-20).
SITE_ACCESS_VALUES = ("basic", "open")
#: `MY_PIPELINE`: `off` (the default: /my and /v1/my answer 404) or `on` (My pipeline, the private
#: Tracker, docs/19 §11). Read by api and web from the env file; anything but `on` keeps it off.
MY_PIPELINE_VALUES = ("off", "on")
#: `GRID_LAYERS_REGIONS`: the states whose grid, flood and prospect files the server's monthly
#: `context_build` pulls (docs/25 §2.7 limit 3; `pipeline/context/osm_power.py` `default_regions`).
#: A comma list of ISO 3166-2 codes or `all`; unset renders the beta's value, Texas. The codes are the
#: Geofabrik extracts the grid build reads (`osm_power.US_STATE_EXTRACTS`; infra/ci/test_beta.py holds
#: the two equal), written here because this module runs on the runner's bare `python3`.
DEFAULT_GRID_LAYERS_REGIONS = "US-TX"
GRID_LAYERS_STATES = frozenset(
    f"US-{code}"
    for code in (
        "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
        "NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY"
    ).split()
)
#: `all` is accepted (the spec allows it) but warned about: on the one-server beta the national line
#: index needs about 1.3 GB more per API process than its 1 GB container (docs/25 §2.7 limit 3), and a
#: national site-finder run is estimated at ten times Texas's, past `SITE_FINDER_TIMEOUT_S`.
GRID_LAYERS_ALL_WARNING = (
    "national: does not fit the one-server beta (about 1.3 GB more per API process, and a site-finder "
    "run of about ten times Texas's; docs/25 section 2.7, limit 3). Leave it unset for US-TX"
)
#: `PARCELS_COUNTIES`: the Texas counties whose land parcels the server's monthly `context_build` pulls
#: from the private bucket (`pipeline/context/txgio_parcels.py` `wanted_counties`; docs/25-siting-parcels.md
#: §8). `all` (every county parcels-layer.yml has published; the pull answers not_published until the
#: first build) or comma-separated county FIPS codes; unset renders `all`. The code rule is the pull's
#: `FIPS_RE` (infra/ci/test_beta.py holds the two equal), written here because this module runs on the
#: runner's bare `python3`.
DEFAULT_PARCELS_COUNTIES = "all"
PARCELS_FIPS = re.compile(r"^48\d{3}$")
#: docs/00-PLAN.md decisions log, 2026-09-18.
DEFAULT_SENDER_LEGAL_NAME = "Compass International Trading Group, LLC"
#: Cost of the gate's bcrypt hash: `caddy hash-password`'s default, which docs/64 documents.
BCRYPT_COST = 14
#: The application secrets OpenTofu generates (infra/terraform/generated.tf), by env-file key.
GENERATED_KEYS = ("POSTGRES_PASSWORD", "SESSION_SECRET", "API_INTERNAL_TOKEN", "AUDIT_HASH_PEPPER")
#: Only DNS records may be replaced by a plan: every other resource holds data or identity (the VM
#: and its database, the buckets, the generated secrets and the key the VM accepts).
REPLACEABLE_TYPES = frozenset({"cloudflare_record"})
#: The beta's VM: compose limits sum to 4.25 GB (docs/64 §1); the docs size it at 4 vCPU / 8 GB.
#: The cap admits cpx31 in ash at 73.49 a month gross (Hetzner API, 2026-10-19; owner approved).
MIN_CORES, MIN_MEMORY_GB, MAX_MONTHLY_PRICE = 4, 8.0, 80.0

HOSTNAME = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
HEX32 = re.compile(r"[0-9a-f]{32}")
GATE_USER = re.compile(r"[A-Za-z0-9._@-]{1,64}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
TAG = re.compile(r"sha-[0-9a-f]{7}|v[0-9A-Za-z._-]+")
#: preflight's placeholder pattern (infra/scheduler/preflight.py PLACEHOLDER), so the workflow
#: refuses what check 9 would fail.
PLACEHOLDER = re.compile(r"replace[\s_-]*me|\[[A-Z_]+ unset\]|\bST 00000\b", re.IGNORECASE)
#: Values written bare in the env file. Anything else is quoted (dotenv_line).
BARE = re.compile(r"[A-Za-z0-9_./:@+=,%-]*")
BCRYPT_ALPHABET = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


class InputError(Exception):
    """Something the owner or the coordinator must change; the message says what."""


def error(message: str) -> None:
    """A GitHub annotation: listed on the run, readable through the API."""
    sys.stdout.write(f"::error::{message}\n")


def warning(message: str) -> None:
    """A GitHub warning annotation: shown on the run, which still goes on."""
    sys.stdout.write(f"::warning::{message}\n")


def summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown.rstrip("\n") + "\n\n")


def cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


# ------------------------------------------------------------------------------ the code's constants
def code_constant(source: str, name: str) -> str | None:
    """The string literal a module assigns to `name` at top level, or None (absent, or not a
    literal). Read with `ast`, so the runner needs none of the application's dependencies."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            return None
    return None


# ------------------------------------------------------------------------------ check-inputs
@dataclass(frozen=True)
class Row:
    name: str
    kind: str
    status: str
    problem: str | None = None
    #: Accepted, but worth a look: shown in the summary and as a `::warning::`, never a stop.
    warning: str | None = None


def check_inputs(
    env: Mapping[str, str],
    *,
    tag: str,
    commit: str,
    code_domain: str | None,
    code_product_name: str | None,
) -> tuple[list[Row], dict[str, str]]:
    """One row per input, and the resolved values later jobs use (`tag`, `product_name`). A row
    with a problem stops the run before any cloud API is called."""
    rows: list[Row] = []

    def row(name: str, kind: str, problem: str | None, ok: str = "set") -> Row:
        if problem == "missing":
            where = "Secrets" if kind == "secret" else "Variables"
            return Row(name, kind, "missing", f"add it under Environment {where}")
        return Row(name, kind, "invalid" if problem else ok, problem)

    for name in SECRETS:
        rows.append(row(name, "secret", _secret_problem(name, env.get(name, ""))))
    for name in VARIABLES:
        rows.append(
            row(name, "variable", _variable_problem(name, env.get(name, ""), tag, commit, code_domain))
        )

    domain = env.get("DOMAIN", "") or "<DOMAIN>"
    legal = env.get("SENDER_LEGAL_NAME", "")
    rows.append(
        row(
            "SENDER_LEGAL_NAME",
            "variable, optional",
            _free_text_problem(legal) if legal else None,
            "set" if legal else f"default: {DEFAULT_SENDER_LEGAL_NAME}",
        )
    )
    contact = env.get("PRODUCT_CONTACT_EMAIL", "")
    rows.append(
        row(
            "PRODUCT_CONTACT_EMAIL",
            "variable, optional",
            f"{contact!r} is not an email address" if contact and not EMAIL.fullmatch(contact) else None,
            "set" if contact else f"default: hello@{domain}",
        )
    )
    product = env.get("PRODUCT_NAME", "").strip()
    if product:
        product_problem = _free_text_problem(product)
    elif code_product_name:
        product_problem = None
    else:
        product_problem = (
            "unset, and DEFAULT_PRODUCT_NAME could not be read from services/social/editorial.py"
        )
    rows.append(
        row(
            "PRODUCT_NAME",
            "variable, optional",
            product_problem,
            "set" if product else f"default from the code: {code_product_name}",
        )
    )
    access = env.get("SITE_ACCESS", "").strip().lower()
    rows.append(
        row(
            "SITE_ACCESS",
            "variable, optional",
            f"{access!r} is not one of {', '.join(SITE_ACCESS_VALUES)}"
            if access and access not in SITE_ACCESS_VALUES
            else None,
            access if access else "default: basic (the shared login)",
        )
    )
    pipeline = env.get("MY_PIPELINE", "").strip().lower()
    rows.append(
        row(
            "MY_PIPELINE",
            "variable, optional",
            f"{pipeline!r} is not one of {', '.join(MY_PIPELINE_VALUES)}"
            if pipeline and pipeline not in MY_PIPELINE_VALUES
            else None,
            pipeline if pipeline else "default: off (My pipeline answers 404)",
        )
    )
    regions_raw = env.get("GRID_LAYERS_REGIONS", "")
    regions, regions_problem = grid_layers_regions(regions_raw)
    regions_row = row(
        "GRID_LAYERS_REGIONS",
        "variable, optional",
        regions_problem,
        regions if regions_raw.strip() else f"default: {DEFAULT_GRID_LAYERS_REGIONS} (Texas)",
    )
    if regions == "all" and not regions_problem:
        regions_row = replace(regions_row, warning=GRID_LAYERS_ALL_WARNING)
    rows.append(regions_row)
    counties_raw = env.get("PARCELS_COUNTIES", "")
    counties, counties_problem = parcels_counties(counties_raw)
    rows.append(
        row(
            "PARCELS_COUNTIES",
            "variable, optional",
            counties_problem,
            counties
            if counties_raw.strip()
            else f"default: {DEFAULT_PARCELS_COUNTIES} (every county published)",
        )
    )
    ercot_set = [name for name in OPTIONAL_SECRETS if env.get(name, "")]
    for name in OPTIONAL_SECRETS:
        value = env.get(name, "")
        if value:
            problem = _secret_problem(name, value)
        elif ercot_set:
            problem = f"set all three ERCOT API values or none ({', '.join(ercot_set)} set)"
        else:
            problem = None
        rows.append(
            row(
                name,
                "secret, optional",
                problem,
                "set" if value else "not set: ERCOT prices use the public downloads",
            )
        )

    if not TAG.fullmatch(tag):
        tag_problem: str | None = (
            "use sha-<7-character sha> (release-images publishes one per green main commit) or a v* tag"
        )
    elif not commit:
        tag_problem = "names no commit in this repository"
    else:
        tag_problem = None
    rows.append(row("tag", "workflow input", tag_problem, f"{tag} ({commit[:12]})"))
    return rows, {"tag": tag, "product_name": product or code_product_name or ""}


def _secret_problem(name: str, value: str) -> str | None:
    if not value:
        return "missing"
    if value != value.strip():
        return "starts or ends with whitespace (a pasted line break?): paste it again without"
    if name == "SITE_ACCESS_PASSWORD":
        if len(value) < 12:
            return "shorter than 12 characters"
        if len(value.encode("utf-8")) > 72:
            return "longer than 72 bytes, which bcrypt cannot hash"
    return None


def _variable_problem(name: str, value: str, tag: str, commit: str, code_domain: str | None) -> str | None:
    if not value.strip():
        return "missing"
    if name == "DOMAIN":
        if not HOSTNAME.fullmatch(value):
            return f"{value!r} is not a lowercase domain name such as example.org"
        if code_domain is None:
            return f"services.api.common.DOMAIN could not be read at {tag} ({commit[:12] or 'no commit'})"
        if value != code_domain.strip().lower():
            return (
                f"{value} differs from services.api.common.DOMAIN = {code_domain} at {tag}: every "
                "email's links and From address use the code's (preflight check 10). Deploy a tag "
                "built with that DOMAIN, or set the variable to it"
            )
    if name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID") and not HEX32.fullmatch(value):
        return "not a 32-character lowercase hex id (Cloudflare: the domain's Overview page, API section)"
    if name == "SITE_ACCESS_USER" and not GATE_USER.fullmatch(value):
        return "use 1-64 letters, digits or . _ @ - (Caddy reads it unquoted)"
    if name == "SENDER_POSTAL_ADDRESS":
        return _free_text_problem(value)
    return None


def grid_layers_regions(value: str) -> tuple[str, str | None]:
    """(the value the env file carries, a problem or None). Unset or blank is the beta's
    `DEFAULT_GRID_LAYERS_REGIONS`; `all` (any case) stays `all`; otherwise each comma-separated
    ISO 3166-2 code is upper-cased, duplicates dropped, order kept, and must name a state the grid
    build has: `US-TX`, not `TX` or `Texas`, so the pull never refuses what this check let through."""
    text = value.strip()
    if not text:
        return DEFAULT_GRID_LAYERS_REGIONS, None
    if text.lower() == "all":
        return "all", None
    codes: list[str] = []
    for part in text.split(","):
        code = part.strip().upper()
        if code and code not in codes:
            codes.append(code)
    if not codes:
        return "", "names no state: use ISO codes such as US-TX (comma separated), or all"
    unknown = [c for c in codes if c not in GRID_LAYERS_STATES]
    if unknown:
        hints = [f"US-{c}" for c in unknown if f"US-{c}" in GRID_LAYERS_STATES]
        hint = f" (did you mean {', '.join(hints)}?)" if hints else ""
        return "", (
            f"{', '.join(unknown)} not a US state's ISO 3166-2 code{hint}: use codes such as US-TX "
            "(comma separated), or all"
        )
    return ",".join(codes), None


def parcels_counties(value: str) -> tuple[str, str | None]:
    """(the value the env file carries, a problem or None). Unset or blank is
    `DEFAULT_PARCELS_COUNTIES`; `all` (any case) stays `all`; otherwise each comma-separated code is
    stripped, duplicates dropped, order kept, and must be a Texas county FIPS code (`48` and three
    digits: `48351`, not `351` or `Newton`), so the pull never refuses what this check let through."""
    text = value.strip()
    if not text:
        return DEFAULT_PARCELS_COUNTIES, None
    if text.lower() == "all":
        return "all", None
    codes: list[str] = []
    for part in text.split(","):
        code = part.strip()
        if code and code not in codes:
            codes.append(code)
    if not codes:
        return "", "names no county: use Texas county FIPS codes such as 48351 (comma separated), or all"
    unknown = [c for c in codes if not PARCELS_FIPS.match(c)]
    if unknown:
        hints = [f"48{c}" for c in unknown if re.fullmatch(r"\d{3}", c)]
        hint = f" (did you mean {', '.join(hints)}?)" if hints else ""
        return "", (
            f"{', '.join(unknown)} not a Texas county FIPS code{hint}: use codes such as 48351 "
            "(comma separated), or all"
        )
    return ",".join(codes), None


def _free_text_problem(value: str) -> str | None:
    if PLACEHOLDER.search(value):
        return "still the template's placeholder"
    try:
        dotenv_line("X", value.strip())
    except InputError as exc:
        return str(exc).removeprefix("X: ")
    return None


def inputs_markdown(rows: Sequence[Row]) -> str:
    problems = [r for r in rows if r.problem]
    lines = [
        "### deploy-beta: inputs in the GitHub Environment `production`",
        "",
        "Settings > Environments > production. Secret values are never shown, only whether they are set.",
        "",
        "| Name | Kind | Status | What to do |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{r.name}` | {r.kind} | {cell(r.status)} | {cell(r.problem or r.warning or '')} |" for r in rows
    ]
    lines += ["", f"**{len(problems)} to fix; nothing was deployed.**" if problems else "All inputs present."]
    return "\n".join(lines)


# ------------------------------------------------------------------------------ OpenTofu
def tfvars(env: Mapping[str, str], *, server_type: str, bucket_prefix: str) -> dict[str, Any]:
    """The beta's variables (infra/terraform/variables.tf, generated.tf). Secrets are not
    variables: the provider tokens come from the environment, and generated.tf makes the rest."""
    return {
        "environment": "production",
        "domain": env["DOMAIN"],
        "cloudflare_zone_id": env["CLOUDFLARE_ZONE_ID"],
        "cloudflare_account_id": env["CLOUDFLARE_ACCOUNT_ID"],
        "single_host": True,
        "vm_backups": False,  # owner, 2026-10-19: the nightly pg_dump to R2 is the database's copy
        "generate_secrets": True,
        "dns_proxied": False,
        "ssh_public_key": "",
        "app_server_type": server_type,
        "r2_bucket_name": bucket_prefix,
    }


def _is_past(timestamp: object, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(timestamp)) <= now
    except ValueError:
        return False


def server_type_verdicts(
    server_types: Mapping[str, Any], *, location: str, now: datetime
) -> list[tuple[str, tuple[int, int, float, str] | None, str]]:
    """`(name, sort key or None, why)` for every x86 type with at least MIN_CORES and MIN_MEMORY_GB.
    A key means it can be ordered in `location` now; None says why not. Availability and deprecation
    are per location (`server_types[].locations[]`): `GET /datacenters` answers 410 Gone since
    1 October 2026, and the top-level `deprecation` gave way to the per-location one
    (https://docs.hetzner.cloud/changelog). A deprecated type stays orderable until its
    `unavailable_after`; it ranks after every current type of the same CPU kind."""
    rows: list[tuple[str, tuple[int, int, float, str] | None, str]] = []
    for st in server_types.get("server_types", []):
        if st.get("architecture", "x86") != "x86":
            continue
        if int(st.get("cores", 0)) < MIN_CORES or float(st.get("memory", 0)) < MIN_MEMORY_GB:
            continue
        name = str(st.get("name"))
        here = next((loc for loc in st.get("locations") or [] if loc.get("name") == location), None)
        if here is None:
            rows.append((name, None, f"not sold in {location}"))
            continue
        if here.get("available") is not True:
            rows.append((name, None, f"out of stock in {location} (available={here.get('available')!r})"))
            continue
        deprecation = here.get("deprecation") or {}
        if deprecation and _is_past(deprecation.get("unavailable_after"), now):
            rows.append((name, None, f"retired in {location} since {deprecation.get('unavailable_after')}"))
            continue
        price = next((p for p in st.get("prices", []) if p.get("location") == location), None)
        if price is None:
            rows.append((name, None, f"no price for {location}"))
            continue
        monthly = float((price.get("price_monthly") or {}).get("gross", "inf"))
        if monthly > MAX_MONTHLY_PRICE:
            rows.append((name, None, f"{monthly:.2f} a month, over {MAX_MONTHLY_PRICE:g}"))
            continue
        dedicated = 1 if st.get("cpu_type") == "dedicated" else 0
        kind = "dedicated" if dedicated else "shared"
        note = f", deprecated (orderable until {deprecation.get('unavailable_after')})" if deprecation else ""
        rows.append(
            (
                name,
                (dedicated, 1 if deprecation else 0, monthly, name),
                f"{kind}, {monthly:.2f} a month{note}",
            )
        )
    return rows


def pick_server_type(
    server_types: Mapping[str, Any], *, location: str, now: datetime | None = None
) -> tuple[str, str]:
    """`(name, why)`: the cheapest current shared-vCPU x86 type with at least MIN_CORES and
    MIN_MEMORY_GB that `location` can sell now (Hetzner API `GET /server_types`); then a deprecated
    but still orderable one; dedicated vCPUs only when no shared type qualifies. The default cx32
    (variables.tf) is no longer sold: Hetzner stopped orders on 1 January 2026, and US locations sold
    only CPX and CCX before that. The error lists every candidate and why it was refused."""
    rows = server_type_verdicts(server_types, location=location, now=now or datetime.now(UTC))
    candidates = [(key, name, why) for name, key, why in rows if key is not None]
    if not candidates:
        refused = "; ".join(f"{name}: {why}" for name, _, why in rows) or "Hetzner lists none"
        raise InputError(
            f"no x86 server type with >= {MIN_CORES} vCPU and >= {MIN_MEMORY_GB:g} GB under "
            f"{MAX_MONTHLY_PRICE:g} a month is available in {location!r} ({refused})"
        )
    _, name, why = min(candidates)
    return (
        name,
        f"{name}: cheapest orderable type in {location} with >= {MIN_CORES} vCPU / "
        f"{MIN_MEMORY_GB:g} GB ({why})",
    )


def plan_guard(plan: Mapping[str, Any]) -> tuple[list[str], dict[str, int]]:
    """Addresses the plan would delete or replace (refused unless REPLACEABLE_TYPES), and a count
    per action. Reads `tofu show -json`; prints nothing from it but addresses."""
    refused: list[str] = []
    counts: dict[str, int] = {}
    for change in plan.get("resource_changes", []):
        actions = list((change.get("change") or {}).get("actions", []))
        key = "replace" if {"delete", "create"} <= set(actions) else "+".join(actions)
        counts[key] = counts.get(key, 0) + 1
        if ("delete" in actions or "forget" in actions) and change.get("type") not in REPLACEABLE_TYPES:
            refused.append(f"{change.get('address')} ({key})")
    return refused, counts


def sensitive_strings(outputs: Mapping[str, Any]) -> Iterator[str]:
    """Every string inside a sensitive output, and each of its lines (a multi-line key is masked
    line by line, as GitHub matches masks within a line)."""

    def leaves(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for v in value.values():
                yield from leaves(v)
        elif isinstance(value, list):
            for v in value:
                yield from leaves(v)

    for output in outputs.values():
        if isinstance(output, Mapping) and output.get("sensitive"):
            for text in leaves(output.get("value")):
                if text:
                    yield text
                    for line in text.splitlines():
                        if line.strip() and line != text:
                            yield line.strip()


def mask_commands(values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return [f"::add-mask::{value}" for value in seen]


def output_value(outputs: Mapping[str, Any], name: str) -> Any:
    output = outputs.get(name)
    if not isinstance(output, Mapping):
        raise InputError(
            f"the state has no output {name!r}: nothing is deployed yet (run the workflow with action deploy)"
        )
    return output.get("value")


# ------------------------------------------------------------------------------ the env file
def dotenv_line(key: str, value: str) -> str:
    """`KEY=value` that Compose (`--env-file`, `env_file:`), systemd's EnvironmentFile (the backup
    unit) and deploy.sh's `dotenv_value` all read back as `value`: bare when it holds only safe
    characters, else single-quoted (literal for all three), else double-quoted when it holds a `'`
    but nothing a double-quoted value would expand."""
    if any(c in value for c in "\n\r\x00"):
        raise InputError(f"{key}: a line break cannot go in the env file")
    if BARE.fullmatch(value):
        return f"{key}={value}"
    if "'" not in value:
        return f"{key}='{value}'"
    if not any(c in value for c in '"\\$`'):
        return f'{key}="{value}"'
    raise InputError(f"{key}: holds a ' together with one of \" \\ $ `, which the env file cannot carry")


def bcrypt_salt(seed: str) -> str:
    """A bcrypt salt (22 characters of bcrypt's base64) derived from a seed in the state, so the
    same password gives the same hash on every run. Standard base64 of 16 bytes, re-lettered:
    the two alphabets differ only in order, and the padding bits stay zero, which bcrypt requires."""
    raw = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return (
        base64.b64encode(raw)
        .decode("ascii")
        .rstrip("=")
        .translate(str.maketrans(STD_ALPHABET, BCRYPT_ALPHABET))
    )


def site_access_hash(password: str, seed: str, hashpw: Callable[[bytes, bytes], bytes]) -> str:
    """SITE_ACCESS_HASH as docs/64 requires it: base64 of a bcrypt hash (`caddy hash-password |
    base64 -w0`), so no `$` reaches Compose's interpolation. Caddy's basic_auth checks any `$2?$`."""
    secret = password.encode("utf-8")
    if not 0 < len(secret) <= 72:
        raise InputError("SITE_ACCESS_PASSWORD must be 1-72 bytes")
    hashed = hashpw(secret, f"$2b${BCRYPT_COST:02d}${bcrypt_salt(seed)}".encode("ascii"))
    return base64.b64encode(hashed).decode("ascii")


def site_access_pairs(env: Mapping[str, str], site_hash: str) -> list[tuple[str, str]]:
    """The gate's keys: the shared login unless the Environment variable SITE_ACCESS is `open`
    (owner, 2026-10-20: the site is public), when no login reaches the env file at all."""
    if env.get("SITE_ACCESS", "").strip().lower() == "open":
        return [("SITE_ACCESS", "open")]
    return [
        ("SITE_ACCESS", "basic"),
        ("SITE_ACCESS_USER", env["SITE_ACCESS_USER"]),
        ("SITE_ACCESS_HASH", site_hash),
    ]


def my_pipeline(env: Mapping[str, str]) -> str:
    """`on` only when the Environment variable MY_PIPELINE says exactly that (any case); otherwise
    `off`, so a typo or an unset variable keeps the private Tracker answering 404 (fails closed)."""
    return "on" if env.get("MY_PIPELINE", "").strip().lower() == "on" else "off"


def rendered_grid_layers_regions(env: Mapping[str, str]) -> str:
    """`GRID_LAYERS_REGIONS` as the env file carries it: the Environment variable normalised
    (`grid_layers_regions`), Texas when unset. Always rendered, because the server's grid, flood and
    prospect pulls refuse to run without it (docs/25 §2.7 limit 3). check-inputs has already refused
    a malformed value; this refuses one too rather than write it."""
    value, problem = grid_layers_regions(env.get("GRID_LAYERS_REGIONS", ""))
    if problem:
        raise InputError(f"GRID_LAYERS_REGIONS: {problem}")
    return value


def rendered_parcels_counties(env: Mapping[str, str]) -> str:
    """`PARCELS_COUNTIES` as the env file carries it: the Environment variable normalised
    (`parcels_counties`), `all` when unset. Always rendered, because the server's parcels pull refuses
    to run without it (`pipeline/context/txgio_parcels.py` `wanted_counties`). check-inputs has already
    refused a malformed value; this refuses one too rather than write it."""
    value, problem = parcels_counties(env.get("PARCELS_COUNTIES", ""))
    if problem:
        raise InputError(f"PARCELS_COUNTIES: {problem}")
    return value


def render_env(
    env: Mapping[str, str], outputs: Mapping[str, Any], *, site_hash: str
) -> list[tuple[str, str]]:
    """Every key the beta's env file carries, in the order of infra/sops/secrets.example.plain.yaml.
    The docs/64 beta values are fixed here; email, Sentry, Stripe, Attio and the rest are left out on
    purpose (v1: the platform dry-runs or omits them). deploy.sh appends ENVIRONMENT."""
    domain = env["DOMAIN"]
    generated = output_value(outputs, "generated_secrets") or {}
    missing = [k for k in (*GENERATED_KEYS, "SITE_ACCESS_SALT") if not generated.get(k)]
    if missing:
        raise InputError(
            f"the state lacks generated secrets {missing}: was it applied with generate_secrets = true?"
        )
    tile_url = output_value(outputs, "tile_url")
    bucket = output_value(outputs, "r2_bucket_name")
    if not tile_url or not bucket:
        raise InputError(
            "the state has no tile_url or r2_bucket_name: apply with the zone and account ids set"
        )
    password = generated["POSTGRES_PASSWORD"]
    return [
        ("DOMAIN", domain),
        ("PLATFORM_POSTURE", "noncommercial"),
        ("MAP_TILE_URL", str(tile_url)),
        ("PRODUCT_NAME", env["PRODUCT_NAME"].strip()),
        ("PRODUCT_URL", f"https://{domain}"),
        ("PRODUCT_CONTACT_EMAIL", env.get("PRODUCT_CONTACT_EMAIL", "").strip() or f"hello@{domain}"),
        ("SENDER_LEGAL_NAME", env.get("SENDER_LEGAL_NAME", "").strip() or DEFAULT_SENDER_LEGAL_NAME),
        ("SENDER_POSTAL_ADDRESS", env["SENDER_POSTAL_ADDRESS"].strip()),
        *site_access_pairs(env, site_hash),
        ("CSP_MODE", "report"),
        ("MY_PIPELINE", my_pipeline(env)),
        ("GRID_LAYERS_REGIONS", rendered_grid_layers_regions(env)),
        ("PARCELS_COUNTIES", rendered_parcels_counties(env)),
        ("SESSION_SECRET", generated["SESSION_SECRET"]),
        ("API_INTERNAL_TOKEN", generated["API_INTERNAL_TOKEN"]),
        ("AUDIT_HASH_PEPPER", generated["AUDIT_HASH_PEPPER"]),
        ("DATABASE_URL", f"postgresql+psycopg://infraque:{password}@postgres:5432/infraque"),
        ("SNAPSHOT_STORE", "local"),
        ("R2_ACCOUNT_ID", env["CLOUDFLARE_ACCOUNT_ID"]),
        ("R2_BUCKET", str(bucket)),
        ("R2_ACCESS_KEY_ID", env["R2_ACCESS_KEY_ID"]),
        ("R2_SECRET_ACCESS_KEY", env["R2_SECRET_ACCESS_KEY"]),
        ("POSTGRES_PASSWORD", password),
        ("LOG_LEVEL", "info"),
        ("BACKUP_RETENTION_DAYS", "35"),
        # The ERCOT API account, only when the owner has added all three (check_inputs refuses one or two).
        *[
            (name, env[name].strip())
            for name in OPTIONAL_SECRETS
            if all(env.get(n, "") for n in OPTIONAL_SECRETS)
        ],
    ]


ENV_HEADER = (
    "# Rendered by .github/workflows/deploy-beta.yml (infra/ci/beta.py render-env) from the OpenTofu\n"
    "# state and the GitHub Environment `production`; do not edit on the host, rerun the workflow.\n"
    "# Left out on purpose in v1: RESEND_API_KEY (email dry-runs), SENTRY_DSN, Stripe, Attio, Turnstile.\n"
)


def write_private(path: Path, text: str) -> None:
    """Create or replace `path` readable by its owner only, before any byte is written."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


# ------------------------------------------------------------------------------ preflight
def find_report(text: str) -> dict[str, Any] | None:
    """The preflight's `--json` report inside the command's output (Compose may print around it)."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and "verdict" in obj and "checks" in obj:
            return obj
    return None


def preflight_markdown(report: Mapping[str, Any]) -> str:
    totals = report.get("totals") or {}
    lines = [
        f"### Preflight: {report.get('verdict')} "
        f"({totals.get('FAIL', 0)} FAIL, {totals.get('WARN', 0)} WARN, {totals.get('PASS', 0)} PASS "
        f"in {report.get('duration_s')} s)",
        "",
        "NO-GO is expected in v1 while email is not set up (check 9, `RESEND_API_KEY`): read every "
        "other FAIL before inviting anyone (docs/64 §3). This job does not fail on NO-GO.",
        "",
        "| # | Status | Check | Result |",
        "|---:|---|---|---|",
    ]
    for check in report.get("checks", []):
        lines.append(
            f"| {cell(check.get('n', ''))} | {cell(check.get('status', ''))} "
            f"| {cell(check.get('title', ''))} | {cell(check.get('summary', ''))} |"
        )
    todo = [c for c in report.get("checks", []) if c.get("actions")]
    if todo:
        lines += ["", "**What to do**", ""]
        for check in todo:
            for action in check["actions"]:
                lines.append(f"- {cell(check.get('n', ''))} {cell(check.get('title', ''))}: {cell(action)}")
    if report.get("record"):
        lines += ["", f"Record on the host: `{report['record']}`"]
    return "\n".join(lines)


# ------------------------------------------------------------------------------ CLI
def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infra/ci/beta.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check-inputs")
    p.add_argument("--tag", required=True)
    p.add_argument("--commit", default="")
    p.add_argument("--common", help="services/api/common.py at the tag's commit")
    p.add_argument("--editorial", help="services/social/editorial.py at the tag's commit")
    p = sub.add_parser("tfvars")
    p.add_argument("--server-type", required=True)
    p.add_argument("--bucket-prefix", required=True)
    p = sub.add_parser("pick-server-type")
    p.add_argument("server_types")
    p.add_argument("--location", default="ash")
    p = sub.add_parser("plan-guard")
    p.add_argument("plan")
    for name in ("mask", "output", "write-key", "render-env"):
        p = sub.add_parser(name)
        p.add_argument("outputs")
        if name == "output":
            p.add_argument("name")
        if name in ("write-key", "render-env"):
            p.add_argument("path")
    p = sub.add_parser("preflight-summary")
    p.add_argument("stdout")
    p.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args(argv)
    env = os.environ
    out = sys.stdout

    try:
        if args.command == "check-inputs":
            rows, resolved = check_inputs(
                env,
                tag=args.tag,
                commit=args.commit,
                code_domain=code_constant(_read(args.common), "DOMAIN"),
                code_product_name=code_constant(_read(args.editorial), "DEFAULT_PRODUCT_NAME"),
            )
            summary(inputs_markdown(rows))
            for row in rows:
                out.write(
                    f"{row.name:<24} {row.kind:<20} {row.status}{': ' + row.problem if row.problem else ''}\n"
                )
            for row in rows:
                if row.warning and not row.problem:
                    warning(f"{row.name}: {row.warning}")
            problems = [r for r in rows if r.problem]
            for row in problems:
                error(f"{row.name}: {row.problem}")
            if problems:
                error(
                    f"{len(problems)} input(s) to fix in Settings > Environments > production; "
                    "nothing was deployed"
                )
                return 1
            github_output = env.get("GITHUB_OUTPUT")
            if github_output:
                with open(github_output, "a", encoding="utf-8") as handle:
                    for key, value in resolved.items():
                        handle.write(f"{key}={value}\n")
            return 0
        if args.command == "tfvars":
            out.write(
                json.dumps(
                    tfvars(env, server_type=args.server_type, bucket_prefix=args.bucket_prefix), indent=1
                )
                + "\n"
            )
            return 0
        if args.command == "pick-server-type":
            types = _load_json(args.server_types)
            for row_name, rank, row_why in server_type_verdicts(
                types, location=args.location, now=datetime.now(UTC)
            ):
                sys.stderr.write(f"  {row_name}: {'ok' if rank else 'refused'}, {row_why}\n")
            name, why = pick_server_type(types, location=args.location)
            sys.stderr.write(why + "\n")
            out.write(name + "\n")
            return 0
        if args.command == "plan-guard":
            refused, counts = plan_guard(_load_json(args.plan))
            out.write(
                "plan: " + (", ".join(f"{n} {a}" for a, n in sorted(counts.items())) or "no changes") + "\n"
            )
            if refused:
                for address in refused:
                    error(f"the plan would delete {address}")
                error(
                    "refusing to apply: this workflow never deletes or replaces the server, its key, the "
                    "buckets or the generated secrets (the database and its password live with them). "
                    "Change the configuration so the plan only adds or updates, or do it by hand (docs/64 §2)"
                )
                return 1
            return 0
        if args.command == "preflight-summary":
            report = find_report(_read(args.stdout))
            if report is None or int(report.get("exit_code", -1)) != args.exit_code:
                error(
                    f"the preflight command failed (exit {args.exit_code}) without a report; its output "
                    "is in this step's log"
                )
                sys.stdout.write(_read(args.stdout)[-4000:])
                return 1
            summary(preflight_markdown(report))
            out.write(f"preflight verdict: {report['verdict']} (exit {args.exit_code}; not a job failure)\n")
            return 0

        outputs: Mapping[str, Any] = _load_json(args.outputs)
        if args.command == "mask":
            out.write("\n".join(mask_commands(sensitive_strings(outputs))) + "\n")
            return 0
        if args.command == "output":
            if (outputs.get(args.name) or {}).get("sensitive"):
                raise InputError(f"{args.name} is sensitive; it is never printed")
            value = output_value(outputs, args.name)
            out.write(("" if value is None else str(value)) + "\n")
            return 0
        if args.command == "write-key":
            key = output_value(outputs, "deploy_ssh_private_key")
            if not key:
                raise InputError(
                    "the state has no deploy_ssh_private_key: apply with generate_secrets = true"
                )
            write_private(Path(args.path), str(key).rstrip("\n") + "\n")
            return 0
        # render-env
        # bcrypt: infra/ci/requirements-deploy.txt, hash-pinned; imported here only (stdlib elsewhere).
        hashpw = cast(Callable[[bytes, bytes], bytes], importlib.import_module("bcrypt").hashpw)
        generated = output_value(outputs, "generated_secrets") or {}
        site_hash = site_access_hash(
            env["SITE_ACCESS_PASSWORD"], str(generated.get("SITE_ACCESS_SALT", "")), hashpw
        )
        raw_hash = base64.b64decode(site_hash).decode("ascii")
        out.write("\n".join(mask_commands([site_hash, raw_hash])) + "\n")
        out.flush()
        pairs = render_env(env, outputs, site_hash=site_hash)
        write_private(Path(args.path), ENV_HEADER + "".join(dotenv_line(k, v) + "\n" for k, v in pairs))
        out.write(f"wrote {len(pairs)} keys to {args.path} (0600): {', '.join(k for k, _ in pairs)}\n")
        return 0
    except InputError as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
