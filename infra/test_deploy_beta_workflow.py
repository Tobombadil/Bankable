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
#: MY_PIPELINE (lane tracker, 2026-10-23): `on` opens the private Tracker; unset or `off` keeps it 404.
#: GRID_LAYERS_REGIONS (lane sf-golive, 2026-10-23): the states the server's grid, flood and prospect
#: pulls load; a variable, never a secret; unset renders US-TX (docs/25 §2.7 limit 3).
OPTIONAL_VARIABLES = {
    "SENDER_LEGAL_NAME",
    "PRODUCT_CONTACT_EMAIL",
    "PRODUCT_NAME",
    "SITE_ACCESS",
    "MY_PIPELINE",
    "GRID_LAYERS_REGIONS",
}
#: The ERCOT Public API account (lane price-hubs, 2026-10-22): optional, all three or none.
OPTIONAL_SECRETS = {"ERCOT_API_SUBSCRIPTION_KEY", "ERCOT_API_USERNAME", "ERCOT_API_PASSWORD"}
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
    expect(secrets == SECRETS | OPTIONAL_SECRETS, f"secrets: {secrets ^ (SECRETS | OPTIONAL_SECRETS)}")
    expect(
        variables == VARIABLES | OPTIONAL_VARIABLES,
        f"variables: {variables ^ (VARIABLES | OPTIONAL_VARIABLES)}",
    )
    check_env = workflow()["jobs"]["inputs"]["steps"][1]["env"]
    expect(
        set(check_env) >= SECRETS | OPTIONAL_SECRETS | VARIABLES | OPTIONAL_VARIABLES,
        "the inputs check sees every input",
    )
    from infra.ci import beta

    expect(set(beta.SECRETS) == SECRETS and set(beta.VARIABLES) == VARIABLES, "beta.py's lists")
    expect(set(beta.OPTIONAL_VARIABLES) == OPTIONAL_VARIABLES, "beta.py's optional list")
    expect(set(beta.OPTIONAL_SECRETS) == OPTIONAL_SECRETS, "beta.py's optional secrets")
    docs = DOCS_64.read_text()
    for name in SECRETS | OPTIONAL_SECRETS | VARIABLES | OPTIONAL_VARIABLES | {"beta-tfstate"}:
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


def test_grid_layers_regions_is_a_variable_that_reaches_the_check_and_the_env_file() -> None:
    """The site finder's go-live (lane sf-golive): GRID_LAYERS_REGIONS travels as an Environment
    variable (`vars.`, never `secrets.`) into the inputs check, which validates it, and into the
    render-env step, which writes it into the env file every container reads (the worker's pulls)."""
    expected = "${{ vars.GRID_LAYERS_REGIONS }}"
    expect("secrets.GRID_LAYERS_REGIONS" not in WORKFLOW.read_text(), "a setting, not a credential")
    jobs = workflow()["jobs"]
    check_env = jobs["inputs"]["steps"][1]["env"]
    expect(check_env.get("GRID_LAYERS_REGIONS") == expected, check_env)
    render = [s for s in jobs["host"]["steps"] if s.get("run") == "infra/ci/beta.sh render-env"]
    expect(len(render) == 1, "one render-env step")
    expect(render[0]["env"].get("GRID_LAYERS_REGIONS") == expected, render[0]["env"])


# ------------------------------------------------------- docs/66 "Site finder go-live" (lane sf-golive)
DOCS_66 = ROOT / "docs" / "66-owner-actions.md"
BUILDS = {
    name: ROOT / ".github" / "workflows" / f"{name}.yml"
    for name in ("grid-layers", "flood-layer", "prospect-layers")
}
BUILD_PROSPECT_SH = ROOT / "infra" / "scripts" / "build_prospect_layers.sh"


def go_live_section() -> str:
    """The section, whitespace folded so a quote wrapped across lines still matches."""
    text = DOCS_66.read_text()
    start = text.index("## Site finder go-live")
    return " ".join(text[start : text.index("\n## ", start + 1)].split())


def test_go_live_names_only_jobs_and_steps_the_build_workflows_have() -> None:
    """Every job or step the owner is told to watch exists under that name."""
    section = go_live_section()
    names: set[str] = set()
    for path in BUILDS.values():
        doc: dict[Any, Any] = yaml.safe_load(path.read_text())
        for job_id, job in doc["jobs"].items():
            names.update({job_id, str(job.get("name", ""))})
            names.update(str(step.get("name", "")) for step in job.get("steps", []))
    cited = re.findall(r'the (?:job|step) "([^"]+)"', section)
    expect(len(cited) >= 3, cited)
    for name in cited:
        expect(name in names, f"docs/66 names {name!r}, which no build workflow has")


def test_go_live_prospect_done_signal_shows_on_the_red_run_it_describes() -> None:
    """Review 2026-10-23: the Texas prospect build is red while PAD-US and Aqueduct have no inbox file,
    and docs/66 said it was done when the step "verify the pointer and its manifest are readable"
    printed a build_id. That step has no status function, so GitHub's implicit success() skips it on
    that red run: the owner would never see the signal. The signal must come from a step that runs
    on a red run, and the line it quotes must be the one build_prospect_layers.sh prints."""
    section = go_live_section()
    item = section[section.index("[prospect-layers](") : section.index("**(c) Optional")]
    expect("Expect a red run" in item, "the item describes the red run")
    doc: dict[Any, Any] = yaml.safe_load(BUILDS["prospect-layers"].read_text())
    job_steps: list[dict[str, Any]] = doc["jobs"]["build-prospect-layers"]["steps"]
    names = [str(step.get("name", "")) for step in job_steps]
    build = next(
        i for i, s in enumerate(job_steps) if s.get("run") == "infra/scripts/build_prospect_layers.sh"
    )
    cited = re.findall(r'Done when the step "([^"]+)"', item)
    expect(len(cited) == 1, cited)
    for name in cited:
        expect(name in names, f"{name!r} is not a step of prospect-layers.yml")
        condition = str(job_steps[names.index(name)].get("if", ""))
        runs_on_red = names.index(name) <= build or "always()" in condition or "failure()" in condition
        expect(runs_on_red, f"{name!r} is skipped once the build step fails (implicit success())")
    template = re.search(r'^log "(published [^"]+ to it)"$', BUILD_PROSPECT_SH.read_text(), re.M)
    expect(template is not None, "build_prospect_layers.sh's publish line")
    line = (template.group(1) if template else "").replace("${PREFIX}", "prospect")
    line = line.replace("${REGION}", "texas").replace("${build_id}", "<build_id>")
    line = line.replace("${pointer_key}", "prospect/current/texas.json")
    expect(f"[prospect-layers] {line}" in item, f"docs/66 quotes the line the script prints: {line}")
