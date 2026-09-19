"""Static checks on infra/compose/*.yml (audit 2026-09-18 §3.1: "the base Compose file is an
invalid project"; "no service declares an env file"). Everything here is checkable with PyYAML
alone; when the Compose CLI is on PATH the same files are also rendered with `docker compose
config` (no daemon needed), which is the authoritative check and is skipped, not faked, without it.
Checks use `expect` instead of bare `assert` (ruff S101, see infra/scheduler/test_jobs.py)."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

COMPOSE_DIR = pathlib.Path(__file__).resolve().parent / "compose"
BASE = COMPOSE_DIR / "docker-compose.yml"
PROD = COMPOSE_DIR / "compose.prod.yml"
APP_SERVICES = ("api", "web", "scheduler", "worker", "browser-worker")
IMAGE_FOR = {"scheduler": "worker"}  # the scheduler runs the worker image with a different command
IMAGE_PREFIX = "${IMAGE_REGISTRY:-ghcr.io/tobombadil}/bankable-"  # .github/workflows/release.yml pushes these
DOCKER = shutil.which("docker")


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


# PyYAML SafeLoader with Compose's `!reset` / `!override` merge tags accepted (value kept, tag
# recorded in `_seen_tags`). Built with `type()` because `yaml` is untyped for mypy --strict.
_seen_tags: list[str] = []
_ComposeLoader: Any = type("ComposeLoader", (yaml.SafeLoader,), {})


def _tagged(tag: str) -> Any:
    def construct(loader: Any, node: Any) -> Any:
        if isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        elif isinstance(node, yaml.MappingNode):
            value = loader.construct_mapping(node)
        else:
            value = loader.construct_scalar(node)
        _seen_tags.append(tag)
        return value

    return construct


_ComposeLoader.add_constructor("!reset", _tagged("!reset"))
_ComposeLoader.add_constructor("!override", _tagged("!override"))


def load(path: pathlib.Path) -> tuple[dict[str, Any], list[str]]:
    _seen_tags.clear()
    doc: dict[str, Any] = yaml.load(path.read_text(), Loader=_ComposeLoader)  # noqa: S506 -- SafeLoader subclass
    return doc, list(_seen_tags)


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    doc, tags = load(BASE)
    expect(tags == [], f"merge tags belong only in compose.prod.yml, found {tags} in the base file")
    return doc


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    doc, _tags = load(PROD)
    return doc


def test_base_services_have_image_and_build_env_file_and_entrypoint(base: dict[str, Any]) -> None:
    services = base["services"]
    expect(set(APP_SERVICES) <= set(services), f"missing services: {set(APP_SERVICES) - set(services)}")
    for name in APP_SERVICES:
        svc = services[name]
        image = svc.get("image", "")
        expected_image = f"{IMAGE_PREFIX}{IMAGE_FOR.get(name, name)}:${{IMAGE_TAG:-latest}}"
        expect(image == expected_image, f"{name}: image {image!r} != {expected_image!r}")
        expect("build" in svc, f"{name}: dev must still be able to build the image locally")
        expect(
            svc.get("env_file") == base["x-env-file"],
            f"{name}: env_file must be the shared x-env-file anchor",
        )
        command = svc.get("command")
        if command is not None:
            expect(command[:3] == ["python", "-m", "infra.entrypoint"], f"{name}: command {command}")
    expect(base["x-env-file"][0]["path"] == "${INFRAQUE_ENV_FILE:-.env}", base["x-env-file"])
    expect(base["x-env-file"][0]["required"] is False, "dev must run without a .env file")


def test_api_and_web_both_receive_the_internal_token(base: dict[str, Any]) -> None:
    for name in ("api", "web"):
        env = base["services"][name]["environment"]
        expect(env.get("API_INTERNAL_TOKEN") == "${API_INTERNAL_TOKEN:-}", f"{name}: {env}")


def test_base_is_a_valid_project_without_the_local_profile(base: dict[str, Any]) -> None:
    """The exact failure the audit reported: a required dependency on the profiled `postgres`
    service makes `docker compose -f docker-compose.yml config` fail with "depends on undefined
    service"; every such dependency must be optional (`required: false`)."""
    services = base["services"]
    expect(services["postgres"].get("profiles") == ["local"], "postgres stays behind the local profile")
    for name, svc in services.items():
        deps = svc.get("depends_on", {})
        if isinstance(deps, dict) and "postgres" in deps:
            expect(deps["postgres"].get("required") is False, f"{name}: postgres dependency must be optional")
    volumes_used = {v.split(":")[0] for s in services.values() for v in s.get("volumes", [])}
    expect(set(base.get("volumes", {})) <= volumes_used, "dangling named volume in the base file")


def test_prod_overrides_only_known_services_and_publishes_only_caddy(
    base: dict[str, Any], prod: dict[str, Any]
) -> None:
    for name, svc in prod["services"].items():
        expect(name in base["services"] or "image" in svc, f"{name}: not in base and defines no image")
        if name != "caddy":
            ports = svc.get("ports", "absent")
            expect(ports in ("absent", []), f"{name}: production must not publish ports ({ports})")
            expect(
                "depends_on" not in svc or svc["depends_on"] == {}, f"{name}: no postgres dependency in prod"
            )
        if name in APP_SERVICES:
            env_file = svc.get("env_file")
            expect(isinstance(env_file, list) and env_file[0]["required"] is True, f"{name}: {env_file}")
            expect(
                env_file[0]["path"] == "${INFRAQUE_ENV_FILE:-/opt/infraque/secrets/.env}",
                f"{name}: env_file path must be the one infra/scripts/deploy.sh writes ({env_file})",
            )
            expect(svc["environment"].get("ENVIRONMENT") == "production", f"{name}: ENVIRONMENT")
    expect("postgres" not in prod["services"], "the local-profile database is never overridden into prod")
    caddy_volumes = {v.split(":")[0] for v in prod["services"]["caddy"]["volumes"]}
    expect(set(prod["volumes"]) <= caddy_volumes, "dangling named volume in the prod file")


def test_prod_uses_merge_tags_only_where_documented(prod: dict[str, Any]) -> None:
    _doc, tags = load(PROD)
    expect(set(tags) <= {"!reset", "!override"}, tags)
    expect(tags.count("!override") >= 5, "env_file and depends_on overrides expected on every app service")


def test_deploy_script_agrees_with_the_compose_files() -> None:
    deploy = (pathlib.Path(__file__).resolve().parent / "scripts" / "deploy.sh").read_text()
    expect('remote_dir="/opt/infraque"' in deploy, "remote_dir")
    expect('env_file="${remote_dir}/secrets/.env"' in deploy, "env_file path")
    expect("INFRAQUE_ENV_FILE=${env_file}" in deploy, "deploy.sh must point env_file: at the file it writes")
    expect("--env-file ${env_file}" in deploy, "deploy.sh must interpolate from the same file")
    expect('image_tag="${2:-${IMAGE_TAG:-latest}}"' in deploy, "IMAGE_TAG env var, default latest")
    expect("--output-type dotenv" in deploy, "the SOPS YAML must be converted to KEY=VALUE lines")


@pytest.mark.skipif(DOCKER is None, reason="docker CLI not on PATH")
def test_docker_compose_config_renders_base_alone_and_with_prod(tmp_path: pathlib.Path) -> None:
    """`docker compose config` needs no daemon. Skipped (visibly) where the CLI is absent."""
    docker = DOCKER or "docker"
    probe = subprocess.run([docker, "compose", "version"], capture_output=True, text=True, check=False)  # noqa: S603
    if probe.returncode != 0:
        pytest.skip("docker compose plugin not available")
    env_file = tmp_path / "fake.env"
    env_file.write_text("DOMAIN=example.test\nSESSION_SECRET=render-only-fake-secret-0123456789abcdef\n")
    base_alone = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [docker, "compose", "-f", str(BASE), "config"], capture_output=True, text=True, check=False
    )
    expect(base_alone.returncode == 0, f"base file must be a valid project on its own: {base_alone.stderr}")
    merged = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [docker, "compose", "-f", str(BASE), "-f", str(PROD), "--env-file", str(env_file), "config"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "INFRAQUE_ENV_FILE": str(env_file), "IMAGE_TAG": "sha-test123"},
    )
    expect(merged.returncode == 0, f"base + prod must render: {merged.stderr}")
    rendered = yaml.safe_load(merged.stdout)
    for name in APP_SERVICES:
        env = rendered["services"][name]["environment"]
        expect(env.get("SESSION_SECRET", "").startswith("render-only"), f"{name}: env file did not reach it")
        expect(
            rendered["services"][name]["image"].endswith(":sha-test123"), rendered["services"][name]["image"]
        )
        expect("ports" not in rendered["services"][name], f"{name}: published ports in prod")
