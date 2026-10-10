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
SINGLE = COMPOSE_DIR / "compose.single.yml"  # one host, the private beta (docs/64)
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
            environment = svc["environment"]
            expect(environment.get("ENVIRONMENT", "").startswith("${ENVIRONMENT:?"), f"{name}: ENVIRONMENT")
            expect(
                environment.get("PLATFORM_POSTURE", "").startswith("${PLATFORM_POSTURE:?"), f"{name}: posture"
            )
            expect(environment.get("BANKABLE_DOMAIN", "").startswith("${DOMAIN:?"), f"{name}: domain")
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


def test_prod_names_no_environment_and_trusts_only_caddy(prod: dict[str, Any]) -> None:
    """Devops audit 2026-09-30 F3: staging applies this file too, so it must not say `production`.
    F1: api and web believe X-Forwarded-For from Caddy's fixed address and nobody else's."""
    import ipaddress

    expect(
        "ENVIRONMENT: production" not in PROD.read_text(), "compose.prod.yml must not hard-code production"
    )
    services = prod["services"]
    caddy_ip = services["caddy"]["networks"]["default"]["ipv4_address"]
    ipam = prod["networks"]["default"]["ipam"]["config"][0]
    subnet, dynamic = ipaddress.ip_network(ipam["subnet"]), ipaddress.ip_network(ipam["ip_range"])
    expect(ipaddress.ip_address(caddy_ip) in subnet, f"{caddy_ip} outside {subnet}")
    expect(ipaddress.ip_address(caddy_ip) not in dynamic, "another container could take Caddy's address")
    for name in ("api", "web"):
        expect(
            services[name]["environment"]["FORWARDED_ALLOW_IPS"] == caddy_ip, f"{name}: FORWARDED_ALLOW_IPS"
        )
    for name in ("scheduler", "worker", "browser-worker"):
        expect("FORWARDED_ALLOW_IPS" not in services[name]["environment"], f"{name} serves no HTTP")
    expect(
        services["caddy"]["environment"]["DOMAIN"].startswith("${DOMAIN:?"),
        "Caddy must not start without DOMAIN",
    )
    expect(
        services["web"]["environment"]["MAP_TILE_URL"].startswith("${MAP_TILE_URL:?"),
        "no OSM fallback in prod",
    )
    expect(services["api"]["environment"]["WEB_CONCURRENCY"] == "1", "one api worker per container (F4)")


def _documented_env(tmp_path: pathlib.Path, environment: str, **overrides: str) -> pathlib.Path:
    """An env file holding exactly the keys infra/sops/secrets.example.plain.yaml marks `required`,
    with its placeholder values, plus the ENVIRONMENT line deploy.sh appends."""
    template = yaml.safe_load((COMPOSE_DIR.parent / "sops" / "secrets.example.plain.yaml").read_text())
    text = (COMPOSE_DIR.parent / "sops" / "secrets.example.plain.yaml").read_text()
    required = [line.split(":", 1)[0] for line in text.splitlines() if "# required" in line]
    values = {key: str(template[key]) for key in required} | {"ENVIRONMENT": environment} | overrides
    env_file = tmp_path / f"{environment}.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))
    return env_file


def _render(env_file: pathlib.Path, *extra: pathlib.Path) -> subprocess.CompletedProcess[str]:
    docker = DOCKER or "docker"
    files = [arg for path in (BASE, PROD, *extra) for arg in ("-f", str(path))]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [docker, "compose", *files, "--env-file", str(env_file), "config"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "INFRAQUE_ENV_FILE": str(env_file), "IMAGE_TAG": "sha-test123"},
    )


def _needs_compose() -> None:
    if DOCKER is None:
        pytest.skip("docker CLI not on PATH")
    probe = subprocess.run([DOCKER, "compose", "version"], capture_output=True, text=True, check=False)  # noqa: S603
    if probe.returncode != 0:
        pytest.skip("docker compose plugin not available")


def test_docker_compose_config_renders_base_alone_and_with_prod(tmp_path: pathlib.Path) -> None:
    """`docker compose config` needs no daemon. Skipped (visibly) where the CLI is absent."""
    _needs_compose()
    docker = DOCKER or "docker"
    base_alone = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [docker, "compose", "-f", str(BASE), "config"], capture_output=True, text=True, check=False
    )
    expect(base_alone.returncode == 0, f"base file must be a valid project on its own: {base_alone.stderr}")
    merged = _render(_documented_env(tmp_path, "production"))
    expect(merged.returncode == 0, f"base + prod must render: {merged.stderr}")
    rendered = yaml.safe_load(merged.stdout)
    for name in APP_SERVICES:
        env = rendered["services"][name]["environment"]
        expect(env.get("SESSION_SECRET", "").startswith("replace-me"), f"{name}: env file did not reach it")
        expect(
            rendered["services"][name]["image"].endswith(":sha-test123"), rendered["services"][name]["image"]
        )
        expect("ports" not in rendered["services"][name], f"{name}: published ports in prod")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_documented_keys_render_a_complete_config(tmp_path: pathlib.Path, environment: str) -> None:
    """Devops audit F3, reproduced: a file with only DATABASE_URL, SESSION_SECRET and
    API_INTERNAL_TOKEN rendered caddy `DOMAIN: ""`, the placeholder domain on every app service,
    no PLATFORM_POSTURE and `ENVIRONMENT: production` everywhere. The documented keys now give a
    non-empty DOMAIN to Caddy and the right values to every service."""
    _needs_compose()
    result = _render(_documented_env(tmp_path, environment))
    expect(result.returncode == 0, result.stderr)
    services = yaml.safe_load(result.stdout)["services"]
    domain = services["caddy"]["environment"]["DOMAIN"]
    expect(domain == "example.com", f"caddy DOMAIN: {domain!r}")
    expect(services["caddy"]["environment"]["ENVIRONMENT"] == environment, services["caddy"]["environment"])
    for name in APP_SERVICES:
        env = services[name]["environment"]
        expect(env["ENVIRONMENT"] == environment, f"{name}: ENVIRONMENT {env['ENVIRONMENT']}")
        expect(env["BANKABLE_DOMAIN"] == domain, f"{name}: BANKABLE_DOMAIN {env['BANKABLE_DOMAIN']}")
        expect(env["PLATFORM_POSTURE"] == "noncommercial", f"{name}: PLATFORM_POSTURE")
    expect(services["web"]["environment"]["MAP_TILE_URL"].startswith("https://"), "MAP_TILE_URL")
    expect(
        services["caddy"]["environment"]["SITE_ACCESS"] == "open", "the access gate is off unless asked for"
    )


@pytest.mark.parametrize("missing", ["DOMAIN", "PLATFORM_POSTURE", "MAP_TILE_URL", "ENVIRONMENT"])
def test_prod_refuses_to_render_without_a_required_key(tmp_path: pathlib.Path, missing: str) -> None:
    _needs_compose()
    for override in (None, ""):
        result = _render(_documented_env(tmp_path, "production", **{missing: override}))  # type: ignore[arg-type]
        expect(result.returncode != 0, f"{missing}={override!r} rendered")
        expect(missing in result.stderr, result.stderr)


SINGLE_DB_PASSWORD = "pw-single"  # noqa: S105 -- a rendering placeholder, never a real password


def test_single_uses_merge_tags_and_touches_only_known_services(base: dict[str, Any]) -> None:
    doc, tags = load(SINGLE)
    expect(set(tags) <= {"!reset", "!override"}, tags)
    expect(set(doc["services"]) <= set(base["services"]), sorted(doc["services"]))
    expect("caddy" not in doc["services"], "the single host serves through the same Caddy as production")


def test_single_host_renders_its_own_database_one_replica_each_and_no_browser(tmp_path: pathlib.Path) -> None:
    """docs/64: base + prod + single on one VM. The database is the base file's PostGIS container,
    reachable from the host's loopback only (the nightly backup runs on the host), every app
    service waits for it, one api and one worker, no browser-worker unless its profile is asked for."""
    _needs_compose()
    env_file = _documented_env(
        tmp_path,
        "production",
        POSTGRES_PASSWORD=SINGLE_DB_PASSWORD,
        SNAPSHOT_STORE="local",
        SITE_ACCESS="basic",
        SITE_ACCESS_USER="beta",
        SITE_ACCESS_HASH="JDJhJDE0JGFiYw==",
    )
    result = _render(env_file, SINGLE)
    expect(result.returncode == 0, result.stderr)
    services = yaml.safe_load(result.stdout)["services"]
    expect(set(services) == {"postgres", "api", "web", "scheduler", "worker", "caddy"}, sorted(services))
    expect(
        services["postgres"]["environment"]["POSTGRES_PASSWORD"] == SINGLE_DB_PASSWORD, "POSTGRES_PASSWORD"
    )
    for port in services["postgres"].get("ports", []):
        expect(port["host_ip"] == "127.0.0.1", f"the database must not listen beyond loopback: {port}")
    for name in ("api", "scheduler", "worker"):
        expect(services[name]["depends_on"]["postgres"]["condition"] == "service_healthy", name)
    for name in ("api", "worker"):
        expect(services[name]["deploy"]["replicas"] == 1, f"{name}: one replica on one host")
    caddy = services["caddy"]["environment"]
    expect((caddy["SITE_ACCESS"], caddy["SITE_ACCESS_USER"]) == ("basic", "beta"), "the gate reaches Caddy")


def test_single_host_refuses_to_render_without_a_database_password(tmp_path: pathlib.Path) -> None:
    _needs_compose()
    for value in (None, ""):
        result = _render(_documented_env(tmp_path, "production", POSTGRES_PASSWORD=value), SINGLE)  # type: ignore[arg-type]
        expect(result.returncode != 0, f"POSTGRES_PASSWORD={value!r} rendered")
        expect("POSTGRES_PASSWORD" in result.stderr, result.stderr)


#: Docker's grace between SIGTERM and SIGKILL for the job runners (docs/51 §2.9 item 2): Procrastinate
#: waits for running jobs on SIGTERM, and Docker's default 10 s killed most of them mid-job, which
#: left them `doing` and their locks held. infra/scheduler/queue_maintenance.py STOP_GRACE_S is the
#: same figure, and its stalled threshold is twice it.
JOB_RUNNERS = ("scheduler", "worker", "browser-worker")


def test_job_runners_get_five_minutes_to_finish_their_jobs(
    base: dict[str, Any], prod: dict[str, Any]
) -> None:
    from infra.scheduler.queue_maintenance import STOP_GRACE_S

    for name in JOB_RUNNERS:
        expect(base["services"][name].get("stop_grace_period") == f"{STOP_GRACE_S}s", f"{name}: grace")
    single, _tags = load(SINGLE)
    for overlay in (prod, single):
        for name, svc in overlay["services"].items():
            expect("stop_grace_period" not in svc, f"{name}: an overlay must not shorten the grace")
    for name in ("api", "web", "caddy"):
        expect("stop_grace_period" not in base["services"].get(name, {}), f"{name} runs no jobs")


def test_the_single_host_renders_the_grace_on_every_job_runner(tmp_path: pathlib.Path) -> None:
    _needs_compose()
    env_file = _documented_env(
        tmp_path, "production", POSTGRES_PASSWORD=SINGLE_DB_PASSWORD, SNAPSHOT_STORE="local"
    )
    result = _render(env_file, SINGLE)
    expect(result.returncode == 0, result.stderr)
    services = yaml.safe_load(result.stdout)["services"]
    for name in ("scheduler", "worker"):
        expect(
            services[name]["stop_grace_period"] == "5m0s",
            f"{name}: {services[name].get('stop_grace_period')}",
        )
