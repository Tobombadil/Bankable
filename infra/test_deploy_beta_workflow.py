""".github/workflows/deploy-beta.yml, the one-server beta's GitHub Actions deploy (docs/64 §2), read
as YAML: its trigger and inputs, the Environment and permissions, pinned actions, the inputs check
ahead of every cloud call, masking, and the exact secret and variable names the owner was given.
infra/ci/test_beta.py runs its steps. Checks use `expect` instead of bare `assert` (ruff S101)."""

from __future__ import annotations

import pathlib
import re
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-beta.yml"
BASEMAP = ROOT / ".github" / "workflows" / "basemap.yml"
BETA_SH = ROOT / "infra" / "ci" / "beta.sh"
BETA_PY = ROOT / "infra" / "ci" / "beta.py"
DOCS_64 = ROOT / "docs" / "64-private-beta.md"
#: The names the owner was given (coordinator, 2026-10-15). Changing one strands the owner's setup.
SECRETS = {
    "HCLOUD_TOKEN",
    "CLOUDFLARE_API_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "SITE_ACCESS_PASSWORD",
}
VARIABLES = {
    "DOMAIN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_ZONE_ID",
    "SITE_ACCESS_USER",
    "SENDER_POSTAL_ADDRESS",
}
OPTIONAL_VARIABLES = {"SENDER_LEGAL_NAME", "PRODUCT_CONTACT_EMAIL", "PRODUCT_NAME", "SITE_ACCESS"}
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


def workflow() -> dict[Any, Any]:
    doc: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return doc


def triggers(doc: dict[Any, Any]) -> dict[str, Any]:
    on: dict[str, Any] = doc["on"] if "on" in doc else doc[True]  # YAML 1.1 reads a bare `on` as True
    return on


def steps(doc: dict[Any, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(job, step) for job, body in doc["jobs"].items() for step in body["steps"]]


def test_dispatch_only_with_the_four_actions_and_a_tag() -> None:
    on = triggers(workflow())
    expect(set(on) == {"workflow_dispatch"}, f"manual (or API) dispatch only: {set(on)}")
    inputs = on["workflow_dispatch"]["inputs"]
    expect(set(inputs) == {"action", "tag"}, inputs)
    expect(inputs["action"]["type"] == "choice", inputs["action"])
    expect(inputs["action"]["options"] == ["deploy", "seed", "preflight", "status"], inputs["action"])
    expect(inputs["action"]["default"] == "status", "the default changes nothing")
    expect(inputs["tag"]["type"] == "string" and inputs["tag"]["default"] == "", inputs["tag"])


def test_one_run_at_a_time_with_minimal_permissions_in_the_production_environment() -> None:
    doc = workflow()
    # actions: read lists release-images runs: an empty tag resolves to the newest build (default_tag).
    expect(
        doc["permissions"] == {"contents": "read", "packages": "read", "actions": "read"}, doc["permissions"]
    )
    expect(
        doc["concurrency"]["cancel-in-progress"] is False and doc["concurrency"]["group"], doc["concurrency"]
    )
    for name, job in doc["jobs"].items():
        expect("permissions" not in job, f"{name} widens the permissions")
        expect(job.get("environment") == "production", f"{name}: environment")
        expect(isinstance(job.get("timeout-minutes"), int), f"{name}: timeout-minutes")


def test_every_action_is_pinned_by_commit_sha() -> None:
    text = WORKFLOW.read_text()
    uses = [step["uses"] for _job, step in steps(workflow()) if "uses" in step]
    expect(bool(uses), "no actions?")
    for ref in uses:
        expect(PINNED.match(ref) is not None, f"{ref} is not pinned to a 40-character commit SHA")
        expect(
            re.search(re.escape(ref) + r" # v\d+\.\d+\.\d+", text) is not None,
            f"{ref}: name the version in a comment",
        )


def test_no_job_reaches_a_cloud_before_the_inputs_check() -> None:
    jobs = workflow()["jobs"]

    def depends_on_inputs(name: str, seen: frozenset[str] = frozenset()) -> bool:
        needs = jobs[name].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        return "inputs" in needs or any(depends_on_inputs(n, seen | {name}) for n in needs if n not in seen)

    for name in jobs:
        if name != "inputs":
            expect(depends_on_inputs(name), f"{name} could run before the inputs check")
    inputs_runs = [s.get("run", "") for s in jobs["inputs"]["steps"]]
    expect(inputs_runs.count("infra/ci/beta.sh check-inputs") == 1, inputs_runs)
    expect("deploy" in jobs["infra"]["if"], "the infra job runs for deploy only")
    expect(jobs["basemap"].get("continue-on-error") is True, "a basemap failure does not fail the deploy")


def test_secrets_and_variables_are_exactly_the_documented_names() -> None:
    text = WORKFLOW.read_text()
    secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) - {"GITHUB_TOKEN"}
    variables = set(re.findall(r"vars\.([A-Z0-9_]+)", text))
    expect(secrets == SECRETS, f"secrets: {secrets ^ SECRETS}")
    expect(
        variables == VARIABLES | OPTIONAL_VARIABLES,
        f"variables: {variables ^ (VARIABLES | OPTIONAL_VARIABLES)}",
    )
    check_env = workflow()["jobs"]["inputs"]["steps"][1]["env"]
    expect(set(check_env) >= SECRETS | VARIABLES | OPTIONAL_VARIABLES, "the inputs check sees every input")
    from infra.ci import beta

    expect(set(beta.SECRETS) == SECRETS and set(beta.VARIABLES) == VARIABLES, "beta.py's lists")
    expect(set(beta.OPTIONAL_VARIABLES) == OPTIONAL_VARIABLES, "beta.py's optional list")
    docs = DOCS_64.read_text()
    for name in SECRETS | VARIABLES | OPTIONAL_VARIABLES | {"beta-tfstate"}:
        expect(f"`{name}`" in docs, f"docs/64 does not tell the owner about {name}")


def test_secrets_reach_only_the_steps_that_use_them() -> None:
    doc = workflow()
    for name, job in doc["jobs"].items():
        job_env = " ".join(str(v) for v in (job.get("env") or {}).values())
        expect("secrets." not in job_env, f"{name}: secrets belong in step env, not job env")
    for job, step in steps(doc):
        if "uses" in step:
            expect(
                "secrets." not in str(step.get("with", "")) + str(step.get("env", "")),
                f"{job}: {step['uses']}",
            )


def test_values_are_masked_and_nothing_traces() -> None:
    sh = BETA_SH.read_text()
    expect("::add-mask::" in BETA_PY.read_text(), "beta.py emits the masks")
    outputs = sh.split("cmd_outputs() {", 1)[1].split("\n}\n", 1)[0]
    mask_at = outputs.index("beta.py mask")
    expect(mask_at < outputs.index("beta.py output"), "mask before any value is printed")
    expect(mask_at < outputs.index("write-key"), "mask before the key is written")
    render = BETA_PY.read_text().split("# render-env", 1)[1]
    expect(render.index("mask_commands") < render.index("write_private"), "the gate hash is masked first")
    for path in (
        WORKFLOW,
        BETA_SH,
        ROOT / "infra" / "ci" / "seed_on_host.sh",
        ROOT / "infra" / "scripts" / "deploy.sh",
    ):
        text = path.read_text()
        expect(
            not re.search(r"set\s+-[a-zA-Z]*x|set -o xtrace|bash -x", text), f"{path.name} traces commands"
        )
    expect("set -euo pipefail" in sh, "beta.sh stops on the first error")


def test_downloads_are_pinned_and_checksummed() -> None:
    sh = BETA_SH.read_text()
    for name in ("TOFU", "PMTILES"):
        version = re.search(rf'^{name}_VERSION="(\d+\.\d+\.\d+)"', sh, re.M)
        digest = re.search(rf'^{name}_SHA256="([0-9a-f]{{64}})"', sh, re.M)
        expect(version is not None and digest is not None, f"{name}: exact version and SHA-256")
    expect("sha256sum -c" in sh, "the archive is verified before it is unpacked")
    requirements = (ROOT / "infra" / "ci" / "requirements-deploy.txt").read_text()
    expect(
        re.search(r"^bcrypt==\d+\.\d+\.\d+ \\$", requirements, re.M) is not None
        and "--hash=sha256:" in requirements,
        requirements,
    )
    expect("--require-hashes" in sh and "--only-binary=:all:" in sh, "pip installs hash-checked wheels only")


def test_the_state_backend_is_r2_and_only_the_workflow_copies_it_in() -> None:
    backend = (ROOT / "infra" / "ci" / "backend_r2.tf").read_text()
    for flag in (
        "skip_region_validation",
        "skip_credentials_validation",
        "skip_requesting_account_id",
        "skip_metadata_api_check",
        "skip_s3_checksum",
        "use_path_style",
    ):
        expect(re.search(rf"^\s*{flag}\s*=\s*true$", backend, re.M) is not None, f"backend: {flag}")
    expect('backend "s3"' in backend, backend)
    expect(not list((ROOT / "infra" / "terraform").glob("backend*.tf")), "local use keeps its local state")
    expect('STATE_BUCKET="beta-tfstate"' in BETA_SH.read_text(), "the bucket the owner creates")


def test_basemap_yml_steps_that_call_the_aws_cli_pass_it_the_r2_key() -> None:
    # The CLI reads only AWS_*; basemap.yml's bucket check ran unsigned and called an existing bucket
    # missing (2026-10-19). build_basemap.sh maps R2_* to AWS_* itself.
    doc: dict[Any, Any] = yaml.safe_load(BASEMAP.read_text())
    calls = 0
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if "aws s3" in str(step.get("run", "")):
                calls += 1
                env = step.get("env") or {}
                expect(
                    env.get("AWS_ACCESS_KEY_ID") == "${{ secrets.R2_ACCESS_KEY_ID }}"
                    and env.get("AWS_SECRET_ACCESS_KEY") == "${{ secrets.R2_SECRET_ACCESS_KEY }}",
                    f"{step.get('name')}: {sorted(env)}",
                )
    expect(calls == 2, f"the bucket check and the verification: {calls}")


def test_basemap_yml_reads_the_same_environment_values() -> None:
    text = BASEMAP.read_text()
    expect(
        "vars.CLOUDFLARE_ACCOUNT_ID" in text and "secrets.R2_ACCOUNT_ID" in text, "both account id sources"
    )
    expect("vars.R2_TILES_BUCKET" in text and "secrets.R2_TILES_BUCKET" in text, "both tiles bucket sources")
    prefix = re.search(r'^BUCKET_PREFIX="([^"]+)"', BETA_SH.read_text(), re.M)
    expect(prefix is not None, "beta.sh BUCKET_PREFIX")
    expect(
        f"'{prefix.group(1) if prefix else ''}-tiles-production'" in text,
        "the fallback is the bucket deploy-beta makes",
    )
