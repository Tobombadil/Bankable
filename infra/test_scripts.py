"""infra/scripts/*.sh: syntax (`bash -n`) for every script, and behavioural runs of deploy.sh,
rollback.sh and backup.sh against PATH shims that record every ssh/scp/sops/aws/pg_dump call
(no network, no Docker, no real key anywhere). Checks use `expect` instead of bare `assert`
(ruff S101, see infra/scheduler/test_jobs.py)."""

from __future__ import annotations

import base64
import datetime as dt
import os
import pathlib
import shutil
import stat
import subprocess
from collections.abc import Iterator

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "infra" / "scripts"
BASH = shutil.which("bash") or "/bin/bash"

SSH_SHIM = r"""#!/usr/bin/env bash
# Records the remote command; answers the few queries deploy.sh reads back.
printf 'ssh %s\n' "$*" >> "$FAKE_LOG"
case "$*" in  # swallow piped stdin (secrets, tokens); keep what would land in the secrets file
  *"cat > /opt/infraque/secrets/.env"*) cat >> "${FAKE_SECRETS:-/dev/null}" ;;
  *) cat > /dev/null ;;
esac
healthy_body='{"status":"ok","checks":{"database":true,"queue":true}}'
case "$*" in
  *"procrastinate_jobs"*) printf '%s\n' "${FAKE_PENDING:-0}" ;;  # before current-tag: seed's compose reads it
  *"cat /opt/infraque/current-tag"*) printf '%s\n' "${FAKE_PREVIOUS_TAG:-}" ;;
  *"docker inspect"*) printf '%s\n' "${FAKE_HEALTH:-healthy}" ;;
  *"/v1/health"*) printf '%s\n' "${FAKE_HEALTH_BODY:-$healthy_body}" ;;
esac
exit 0
"""
LOGGING_SHIM = (
    '#!/usr/bin/env bash\nprintf \'{name} %s\\n\' "$*" >> "$FAKE_LOG"\ncat > /dev/null 2>&1 || true\nexit 0\n'
)
#: Every key deploy.sh requires, with obviously fake values (infra/sops/secrets.example.plain.yaml).
REQUIRED_FAKES = {
    "SESSION_SECRET": "fake-session-secret-not-a-secret-0123456789",
    "AUDIT_HASH_PEPPER": "fake-pepper",
    "DATABASE_URL": "postgresql+psycopg://u:p@db.example.test/infraque",
    "DOMAIN": "example.test",
    "PLATFORM_POSTURE": "noncommercial",
    "MAP_TILE_URL": "https://tiles.example.test/basemap.pmtiles",
    "SNAPSHOT_STORE": "s3",
    "R2_ACCOUNT_ID": "fake-account",
    "R2_BUCKET": "fake-bucket",
    "R2_ACCESS_KEY_ID": "fake-key-id",
    "R2_SECRET_ACCESS_KEY": "fake-secret",
    "SENDER_LEGAL_NAME": "Example LLC",
    "SENDER_POSTAL_ADDRESS": "1 Example St",
    "PRODUCT_NAME": "Example",
    "PRODUCT_URL": "https://example.test",
    "PRODUCT_CONTACT_EMAIL": "hello@example.test",
}
SOPS_SHIM = (
    '#!/usr/bin/env bash\nprintf \'sops %s\\n\' "$*" >> "$FAKE_LOG"\n'
    # A non-empty API_INTERNAL_TOKEN unless a test sets FAKE_TOKEN (possibly to ""); deploy.sh needs one.
    'echo "API_INTERNAL_TOKEN=${FAKE_TOKEN-fake-internal-token-not-a-secret}"\n'
    # Every other required key unless named in FAKE_OMIT; FAKE_EXTRA lines come last (and win).
    + "".join(
        f'[[ " ${{FAKE_OMIT:-}} " == *" {k} "* ]] || echo "{k}={v}"\n' for k, v in REQUIRED_FAKES.items()
    )
    + '[[ -n "${FAKE_EXTRA:-}" ]] && printf \'%s\\n\' "$FAKE_EXTRA"\nexit 0\n'
)
PG_SHIMS = {
    "psql": "#!/usr/bin/env bash\necho '16.4'\n",
    "pg_dump": '#!/usr/bin/env bash\nprintf \'pg_dump %s\\n\' "$*" >> "$FAKE_LOG"\n'
    "[[ \"$1\" == --version ]] && { echo 'pg_dump (PostgreSQL) 16.4'; exit 0; }\n"
    'for a in "$@"; do [[ "$a" == --file=* ]] && echo dump > "${a#--file=}"; done\n',
    "aws": '#!/usr/bin/env bash\nprintf \'aws %s\\n\' "$*" >> "$FAKE_LOG"\n'
    '[[ "$2" == ls ]] && cat "$FAKE_R2_LISTING"\nexit 0\n',
}


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


def write_shim(directory: pathlib.Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def shims(tmp_path: pathlib.Path) -> Iterator[tuple[pathlib.Path, dict[str, str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_shim(bin_dir, "ssh", SSH_SHIM)
    write_shim(bin_dir, "scp", LOGGING_SHIM.format(name="scp"))
    write_shim(bin_dir, "sops", SOPS_SHIM)
    for name, body in PG_SHIMS.items():
        write_shim(bin_dir, name, body)
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
        "APP_HOST": "10.0.1.10",
        "WORKER_HOSTS": "10.0.1.20 10.0.1.21",
        "BROWSER_WORKER_HOST": "10.0.1.30",
        "SOPS_AGE_KEY": "AGE-SECRET-KEY-1FAKEFAKEFAKE",  # obvious fake, never a real key
        "DEPLOY_LOG": str(tmp_path / "deploy-log.md"),
        "HEALTH_TIMEOUT_SECONDS": "1",
        "HEALTH_INTERVAL_SECONDS": "0",
    }
    yield log, env


def run(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed script path under the repo, no shell
        [BASH, str(SCRIPTS / script), *args], capture_output=True, text=True, cwd=ROOT, env=env, check=False
    )


def calls(log: pathlib.Path) -> list[str]:
    return log.read_text().splitlines()


def first_index(lines: list[str], needle: str) -> int:
    for i, line in enumerate(lines):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} never called; log was:\n" + "\n".join(lines))


@pytest.mark.parametrize("script", sorted(p.name for p in SCRIPTS.glob("*.sh")))
def test_every_script_parses(script: str) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [BASH, "-n", str(SCRIPTS / script)], capture_output=True, text=True, check=False
    )
    expect(result.returncode == 0, result.stderr)
    expect("pipefail" in (SCRIPTS / script).read_text(), f"{script}: set -euo pipefail expected")


def test_deploy_order_sync_pull_stop_migrate_up_health_workers_scheduler(
    shims: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = shims
    result = run("deploy.sh", "staging", "sha-abc1234", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    lines = calls(log)
    app = "ssh root@10.0.1.10"
    sync_app = first_index(lines, "scp ")
    secrets_app = first_index(lines, f"{app} umask 077 && cat > /opt/infraque/secrets/.env")
    compose = (
        "cd /opt/infraque/compose && IMAGE_TAG=sha-abc1234 INFRAQUE_ENV_FILE=/opt/infraque/secrets/.env "
        "docker compose -f docker-compose.yml -f compose.prod.yml --env-file /opt/infraque/secrets/.env"
    )
    pull = first_index(lines, f"{app} {compose} pull")
    stop_worker = first_index(lines, f"ssh root@10.0.1.20 {compose} stop worker")
    stop_scheduler = first_index(lines, f"{app} {compose} stop scheduler")
    migrate = first_index(
        lines, "run --rm --no-deps -T api alembic -c services/db/migrations/alembic.ini upgrade head"
    )
    queue_schema = first_index(lines, f"{app} {compose} run --rm --no-deps -T scheduler python -c ")
    step = lines[queue_schema]
    expect('m = "infra.scheduler.queue_schema"' in step and step.endswith(" ensure"), step)
    up_app = first_index(lines, "up -d --no-build caddy api web")
    health = first_index(lines, "docker inspect -f '{{.State.Health.Status}}'")
    health_url = first_index(lines, "curl -sf --max-time 5 http://api:8000/v1/health")
    up_worker = first_index(lines, "up -d --no-build worker")
    up_browser = first_index(lines, "up -d --no-build browser-worker")
    up_scheduler = first_index(lines, "up -d --no-build scheduler")
    record = first_index(lines, "> /opt/infraque/current-tag")
    order = [sync_app, secrets_app, pull, stop_worker, stop_scheduler, migrate, queue_schema, up_app, health]
    order += [health_url, up_worker, up_browser, up_scheduler, record]
    expect(
        sum("infra.scheduler.queue_schema" in ln for ln in lines) == 1, "queue schema step runs exactly once"
    )
    expect(order == sorted(order), f"deploy steps out of order: {order}\n" + "\n".join(lines))
    sops_call = "--input-type yaml --output-type dotenv infra/sops/secrets.staging.enc.yaml"
    expect(any(sops_call in ln for ln in lines), "sops must emit dotenv lines")
    expect(
        sum("cat > /opt/infraque/secrets/.env" in ln for ln in lines) == 4, "secrets must reach all 4 hosts"
    )
    expect(sum("scp " in ln and "backup.sh" in ln for ln in lines) == 4, "backup.sh must reach all 4 hosts")
    expect("| staging | sha-abc1234 |" in pathlib.Path(env["DEPLOY_LOG"]).read_text(), "deploy log row")


def test_deploy_refuses_to_start_web_without_the_internal_token(
    shims: tuple[pathlib.Path, dict[str, str]],
) -> None:
    """docs/60 §11 item 9: without API_INTERNAL_TOKEN every page `web` renders shares the anonymous
    60/hour bucket and starts returning errors after a few views. The deploy stops before any host
    is touched, not after `web` is up."""
    log, env = shims
    for token in ("", '""'):
        log.write_text("")
        result = run("deploy.sh", "production", "sha-abc1234", env={**env, "FAKE_TOKEN": token})
        expect(result.returncode == 1, result.stdout + result.stderr)
        expect("API_INTERNAL_TOKEN is empty or missing" in result.stderr, result.stderr)
        lines = calls(log)
        expect(not any(ln.startswith(("ssh ", "scp ")) for ln in lines), f"no host may be touched: {lines}")


@pytest.mark.parametrize(
    "key",
    [
        "DOMAIN",
        "PLATFORM_POSTURE",
        "MAP_TILE_URL",
        "AUDIT_HASH_PEPPER",
        "SNAPSHOT_STORE",
        "SENDER_LEGAL_NAME",
    ],
)
def test_deploy_refuses_a_secrets_file_missing_a_required_key(
    shims: tuple[pathlib.Path, dict[str, str]], key: str
) -> None:
    """Devops audit 2026-09-30 F3: a file written from the old documented list had no DOMAIN (Caddy
    crash-looped) and no PLATFORM_POSTURE (the code default published a different source set). The
    deploy now names every missing key and stops before any host is touched."""
    log, env = shims
    for variant in ({"FAKE_OMIT": key}, {"FAKE_EXTRA": f"{key}="}):
        log.write_text("")
        result = run("deploy.sh", "production", "sha-abc1234", env={**env, **variant})
        expect(result.returncode == 1, result.stdout + result.stderr)
        expect("refusing to deploy" in result.stderr and key in result.stderr, result.stderr)
        expect(not any(ln.startswith(("ssh ", "scp ")) for ln in calls(log)), f"{key}: a host was touched")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("PLATFORM_POSTURE=Commercial ", "PLATFORM_POSTURE must be commercial or noncommercial"),
        ("SNAPSHOT_STORE=local", "SNAPSHOT_STORE must be s3"),
        ("ENVIRONMENT=production", "says ENVIRONMENT=production"),
    ],
)
def test_deploy_refuses_wrong_values(
    shims: tuple[pathlib.Path, dict[str, str]], extra: str, message: str
) -> None:
    log, env = shims
    result = run("deploy.sh", "staging", "sha-abc1234", env={**env, "FAKE_EXTRA": extra})
    expect(result.returncode == 1 and message in result.stderr, result.stderr)
    expect(not any(ln.startswith(("ssh ", "scp ")) for ln in calls(log)), "no host may be touched")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deploy_ships_the_environment_it_was_run_for(
    shims: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path, environment: str
) -> None:
    """compose.prod.yml used to hard-code ENVIRONMENT=production, and staging applies that file too.
    Now the env file every host receives ends with exactly one ENVIRONMENT line: the argument."""
    _log, env = shims
    shipped = tmp_path / "shipped.env"
    extra = {"FAKE_EXTRA": f"ENVIRONMENT={environment}"} if environment == "staging" else {}
    result = run("deploy.sh", environment, "sha-abc1234", env={**env, "FAKE_SECRETS": str(shipped), **extra})
    expect(result.returncode == 0, result.stdout + result.stderr)
    per_host = shipped.read_text().split("ENVIRONMENT=" + environment + "\n")
    expect(len(per_host) == 5 and per_host[-1] == "", f"one ENVIRONMENT line, last, on 4 hosts: {per_host}")
    expect(all("ENVIRONMENT=" not in chunk for chunk in per_host), "no second ENVIRONMENT line")
    expect(all("DOMAIN=example.test" in chunk for chunk in per_host[:4]), "the secrets travel unchanged")


def test_required_keys_match_the_documented_template() -> None:
    """deploy.sh's REQUIRED_KEYS and the keys infra/sops/secrets.example.plain.yaml marks `required`
    are one list; so are the keys the test shim provides."""
    import re

    deploy = (SCRIPTS / "deploy.sh").read_text()
    block = re.search(r"REQUIRED_KEYS=\((.*?)\)", deploy, flags=re.DOTALL)
    expect(block is not None, "REQUIRED_KEYS array not found")
    required = set(block.group(1).split()) if block else set()
    template = (ROOT / "infra" / "sops" / "secrets.example.plain.yaml").read_text()
    marked = set(re.findall(r"^([A-Z][A-Z0-9_]*):.*# required\b", template, flags=re.MULTILINE))
    expect(required == marked, f"deploy.sh only: {required - marked}; template only: {marked - required}")
    expect(required == set(REQUIRED_FAKES) | {"API_INTERNAL_TOKEN"}, "SOPS shim out of step")
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*):", template, flags=re.MULTILINE))
    expect("ENVIRONMENT" not in documented, "deploy.sh writes ENVIRONMENT; the file must not carry it")


def test_missing_queue_schema_fails_the_health_gate_and_rolls_back(
    shims: tuple[pathlib.Path, dict[str, str]],
) -> None:
    """If `/v1/health` answers but reports `checks.queue` false (the queue schema step did not take),
    the deploy must not start workers that would crash-loop: it fails and rolls back."""
    log, env = shims
    body = '{"status":"ok","checks":{"database":true,"queue":false}}'
    env = {**env, "FAKE_HEALTH_BODY": body, "FAKE_PREVIOUS_TAG": "sha-good111"}
    result = run("deploy.sh", "staging", "sha-bad0000", env=env)
    expect(result.returncode != 0, result.stdout + result.stderr)
    expect("checks.queue is not true" in result.stdout, result.stdout)
    expect("rolling back to the previous tag sha-good111" in result.stdout, result.stdout)
    lines = calls(log)
    expect(
        not any("IMAGE_TAG=sha-bad0000" in ln and "up -d --no-build worker" in ln for ln in lines),
        "workers of the failed tag must never start",
    )


def test_deploy_defaults_to_image_tag_env_then_latest(shims: tuple[pathlib.Path, dict[str, str]]) -> None:
    log, env = shims
    expect(run("deploy.sh", "production", env=env).returncode == 0, "default tag")
    expect(any("IMAGE_TAG=latest " in ln for ln in calls(log)), "default must be latest")
    log.write_text("")
    expect(run("deploy.sh", "production", env={**env, "IMAGE_TAG": "sha-fromenv"}).returncode == 0, "env tag")
    expect(any("IMAGE_TAG=sha-fromenv " in ln for ln in calls(log)), "IMAGE_TAG env var must be honoured")
    expect(run("deploy.sh", "qa", env=env).returncode == 1, "unknown environment must be refused")


def test_failed_health_check_rolls_back_to_the_previous_tag(
    shims: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = shims
    env = {**env, "FAKE_HEALTH": "unhealthy", "FAKE_PREVIOUS_TAG": "sha-good111"}
    result = run("deploy.sh", "production", "sha-bad0000", env=env)
    expect(result.returncode != 0, "a failed health check must fail the deploy")
    expect("rolling back to the previous tag sha-good111" in result.stdout, result.stdout)
    lines = calls(log)
    up = (
        "docker compose -f docker-compose.yml -f compose.prod.yml --env-file /opt/infraque/secrets/.env up -d"
    )
    bad_up = first_index(
        lines, f"IMAGE_TAG=sha-bad0000 INFRAQUE_ENV_FILE=/opt/infraque/secrets/.env {up} --no-build caddy"
    )
    rollback_up = first_index(
        lines, f"IMAGE_TAG=sha-good111 INFRAQUE_ENV_FILE=/opt/infraque/secrets/.env {up} --no-build caddy"
    )
    expect(bad_up < rollback_up, "rollback must redeploy the previous tag after the failed one")
    # The rollback's own health check also fails here (FAKE_HEALTH is still unhealthy) and must
    # NOT recurse into another rollback: exactly one rollback attempt, then a clean failure.
    expect(sum("up -d --no-build caddy api web" in ln for ln in lines) == 2, "\n".join(lines))
    expect("> /opt/infraque/current-tag" not in "\n".join(lines), "a failed deploy must not record its tag")


def test_rollback_reruns_deploy_without_auto_rollback(shims: tuple[pathlib.Path, dict[str, str]]) -> None:
    log, env = shims
    result = run("rollback.sh", "staging", "sha-good111", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    expect(any("IMAGE_TAG=sha-good111 " in ln for ln in calls(log)), "rollback must deploy the given tag")
    expect(not any("downgrade -1" in ln for ln in calls(log)), "no migration downgrade unless asked")
    log.write_text("")
    result = run("rollback.sh", "staging", "sha-good111", "--downgrade-migration", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    lines = calls(log)
    expect(first_index(lines, "downgrade -1") < first_index(lines, "up -d --no-build caddy api web"), lines)


def test_backup_dumps_uploads_and_prunes_r2_to_14_daily_plus_8_weekly(
    shims: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    log, env = shims
    today = dt.datetime.now(dt.UTC).date()

    def named(days_ago: int) -> str:
        return f"infraque-{(today - dt.timedelta(days=days_ago)).strftime('%Y%m%d')}T031700Z.dump"

    def sunday_at_least(days: int) -> int:
        while (today - dt.timedelta(days=days)).isoweekday() != 7:
            days += 1
        return days

    weekly_keep = sunday_at_least(15)  # a Sunday older than 14 d but inside 8 weeks: kept
    weekly_drop = sunday_at_least(57)  # a Sunday older than 8 weeks: pruned
    daily_drop = weekly_keep + 3  # a weekday older than 14 d: pruned
    listing = tmp_path / "r2-listing.txt"
    objects = (named(1), named(weekly_keep), named(daily_drop), named(weekly_drop), "not-a-backup.txt")
    listing.write_text("".join(f"2026-01-01 03:17:00 1000 {name}\n" for name in objects))
    backup_dir = tmp_path / "backups"
    env = {
        **env,
        "FAKE_R2_LISTING": str(listing),
        "BACKUP_DIR": str(backup_dir),
        "DATABASE_URL": "postgresql+psycopg://u:p@db.example.test:5432/infraque",
        "R2_BUCKET": "fake-bucket",
        "R2_ACCOUNT_ID": "fakeaccount",
        "R2_ACCESS_KEY_ID": "fake-key-id",
        "R2_SECRET_ACCESS_KEY": "fake-secret",
    }
    result = run("backup.sh", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    lines = calls(log)
    expect(
        any("pg_dump --format=custom" in ln and "postgresql://u:p@db.example.test" in ln for ln in lines),
        lines,
    )
    endpoint = "--endpoint-url https://fakeaccount.r2.cloudflarestorage.com"
    expect(any(ln.startswith("aws s3 cp ") and endpoint in ln for ln in lines), lines)
    removed = [ln for ln in lines if ln.startswith("aws s3 rm ")]
    expect(len(removed) == 2, f"expected exactly the two expired objects removed, got {removed}")
    expect(
        any(named(daily_drop) in ln for ln in removed), f"{named(daily_drop)} (daily, >14 d) must be pruned"
    )
    expect(
        any(named(weekly_drop) in ln for ln in removed), f"{named(weekly_drop)} (Sunday, >8 w) must be pruned"
    )
    expect(not any(named(weekly_keep) in ln for ln in removed), f"{named(weekly_keep)} (Sunday, <=8 w) kept")
    expect((backup_dir / "last-success").exists(), "last-success stamp for the backup-age alert")


def test_backup_refuses_a_pg_dump_older_than_the_server(
    shims: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    log, env = shims
    write_shim(tmp_path / "bin", "psql", "#!/usr/bin/env bash\necho '17.2'\n")
    env = {
        **env,
        "BACKUP_DIR": str(tmp_path / "backups"),
        "DATABASE_URL": "postgresql://u:p@db.example.test:5432/infraque",
        "R2_BUCKET": "b",
        "R2_ACCOUNT_ID": "a",
        "R2_ACCESS_KEY_ID": "k",
        "R2_SECRET_ACCESS_KEY": "s",
    }
    result = run("backup.sh", env=env)
    expect(result.returncode == 1 and "install postgresql-client-17" in result.stderr, result.stderr)
    expect(not any("pg_dump --format" in ln for ln in calls(log)), "no dump attempted with a too-old client")


#: A single host's own database (compose.single.yml, docs/64) and the gate's login, as the env file
#: carries them. The hash is base64("$2a$14$...") like the runbook makes it; values are fakes.
SINGLE_HOST_EXTRA = (
    "SNAPSHOT_STORE=local\n"
    "POSTGRES_PASSWORD=fake-db-password\n"
    "DATABASE_URL=postgresql+psycopg://infraque:fake-db-password@postgres:5432/infraque"
)
#: base64 of a bcrypt-shaped fake, built here so no key-shaped literal sits in the source (gitleaks).
FAKE_GATE_HASH = base64.b64encode(b"$2a$14$" + b"fake" * 3).decode()
GATE_EXTRA = f"SITE_ACCESS=basic\nSITE_ACCESS_USER=beta\nSITE_ACCESS_HASH={FAKE_GATE_HASH}"


def _single(env: dict[str, str], extra: str = SINGLE_HOST_EXTRA) -> dict[str, str]:
    single = {k: v for k, v in env.items() if k not in ("WORKER_HOSTS", "BROWSER_WORKER_HOST")}
    return single | {"SINGLE_HOST": "1", "FAKE_EXTRA": extra}


def test_single_host_deploy_runs_everything_on_one_vm_with_its_database_first(
    shims: tuple[pathlib.Path, dict[str, str]],
) -> None:
    log, env = shims
    result = run(
        "deploy.sh", "production", "sha-beta123", env=_single(env, SINGLE_HOST_EXTRA + "\n" + GATE_EXTRA)
    )
    expect(result.returncode == 0, result.stdout + result.stderr)
    lines = calls(log)
    expect(all("10.0.1.2" not in ln and "10.0.1.3" not in ln for ln in lines), "only APP_HOST is touched")
    compose = (
        "docker compose -f docker-compose.yml -f compose.prod.yml -f compose.single.yml "
        "--env-file /opt/infraque/secrets/.env"
    )
    app = "ssh root@10.0.1.10"
    pull = first_index(lines, f"{compose} pull --quiet caddy api web scheduler worker postgres")
    stop_worker = first_index(lines, f"{app} cd /opt/infraque/compose && IMAGE_TAG=sha-beta123")
    db_up = first_index(lines, f"{compose} up -d --no-build postgres")
    db_health = first_index(lines, "ps -q postgres")
    migrate = first_index(lines, "alembic -c services/db/migrations/alembic.ini upgrade head")
    up_app = first_index(lines, "up -d --no-build caddy api web")
    up_worker = first_index(lines, "up -d --no-build worker")
    up_scheduler = first_index(lines, "up -d --no-build scheduler")
    order = [pull, stop_worker, db_up, db_health, migrate, up_app, up_worker, up_scheduler]
    expect(order == sorted(order), f"steps out of order: {order}\n" + "\n".join(lines))
    expect(not any("browser-worker" in ln for ln in lines), "no browser-worker on the single host")
    expect(sum("cat > /opt/infraque/secrets/.env" in ln for ln in lines) == 1, "secrets reach the one host")
    expect(any("scp " in ln and "compose.single.yml" in ln for ln in lines), "the overlay is shipped")
    row = pathlib.Path(env["DEPLOY_LOG"]).read_text()
    expect("| production (single host) | sha-beta123 |" in row, row)


@pytest.mark.parametrize(
    ("extra", "env_change", "message"),
    [
        (
            SINGLE_HOST_EXTRA.replace("POSTGRES_PASSWORD=fake-db-password", "POSTGRES_PASSWORD="),
            {},
            "POSTGRES_PASSWORD",
        ),
        (SINGLE_HOST_EXTRA.replace("@postgres:5432", "@db.example.test:5432"), {}, "DATABASE_URL"),
        (SINGLE_HOST_EXTRA.replace("SNAPSHOT_STORE=local", "SNAPSHOT_STORE=ftp"), {}, "SNAPSHOT_STORE"),
        (SINGLE_HOST_EXTRA, {"WORKER_HOSTS": "10.0.1.20"}, "unset WORKER_HOSTS"),
        (SINGLE_HOST_EXTRA, {"SINGLE_HOST": "yes"}, "SINGLE_HOST must be 1"),
    ],
)
def test_single_host_deploy_refuses_a_database_it_cannot_run(
    shims: tuple[pathlib.Path, dict[str, str]], extra: str, env_change: dict[str, str], message: str
) -> None:
    log, env = shims
    result = run("deploy.sh", "production", "sha-beta123", env=_single(env, extra) | env_change)
    expect(result.returncode != 0, result.stdout)
    expect(message in result.stderr, result.stderr)
    expect(not any("ssh " in ln for ln in calls(log)), "nothing may touch a host before the checks pass")


def test_local_snapshots_are_refused_on_three_hosts(shims: tuple[pathlib.Path, dict[str, str]]) -> None:
    _log, env = shims
    result = run("deploy.sh", "production", "sha-x", env=env | {"FAKE_EXTRA": "SNAPSHOT_STORE=local"})
    expect(result.returncode != 0 and "SNAPSHOT_STORE must be s3" in result.stderr, result.stderr)


@pytest.mark.parametrize(
    ("gate", "message"),
    [
        (f"SITE_ACCESS=basic\nSITE_ACCESS_HASH={FAKE_GATE_HASH}", "SITE_ACCESS_USER"),
        ("SITE_ACCESS=basic\nSITE_ACCESS_USER=beta", "SITE_ACCESS_HASH"),
        ("SITE_ACCESS=basic\nSITE_ACCESS_USER=beta\nSITE_ACCESS_HASH=$2a$14$rawhash", "SITE_ACCESS_HASH"),
        ("SITE_ACCESS=private", "SITE_ACCESS must be open or basic"),
    ],
)
def test_deploy_refuses_an_access_gate_caddy_could_not_enforce(
    shims: tuple[pathlib.Path, dict[str, str]], gate: str, message: str
) -> None:
    log, env = shims
    result = run("deploy.sh", "staging", "sha-x", env=env | {"FAKE_EXTRA": gate})
    expect(result.returncode != 0 and message in result.stderr, result.stderr)
    expect(not any("ssh " in ln for ln in calls(log)), "nothing may touch a host before the checks pass")


def test_backup_on_a_single_host_dumps_the_stack_database_over_loopback(
    shims: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    log, env = shims
    listing = tmp_path / "r2-listing.txt"  # the dump just uploaded is always there
    today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    listing.write_text(f"2026-01-01 03:17:00 1000 infraque-{today}T031700Z.dump\n")
    env = {
        **env,
        "FAKE_R2_LISTING": str(listing),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "DATABASE_URL": "postgresql+psycopg://infraque:fake-db-password@postgres:5432/infraque",
        "R2_BUCKET": "fake-bucket",
        "R2_ACCOUNT_ID": "fakeaccount",
        "R2_ACCESS_KEY_ID": "fake-key-id",
        "R2_SECRET_ACCESS_KEY": "fake-secret",
    }
    result = run("backup.sh", env=env)
    expect(result.returncode == 0, result.stdout + result.stderr)
    dumps = [ln for ln in calls(log) if "pg_dump --format=custom" in ln]
    expect(
        bool(dumps) and "postgresql://infraque:fake-db-password@127.0.0.1:5432/infraque" in dumps[0], dumps
    )


def _data_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "data-root"
    for part in ("runs/us.iso.caiso.gen_queue", "snapshots/us.iso.caiso.gen_queue", "normalized/context"):
        (root / part).mkdir(parents=True)
    (root / "normalized" / "context" / "us.eia.860m.plants.parquet").write_bytes(b"PAR1")
    return root


def test_seed_ships_the_data_root_then_loads_waits_loads_context_and_fetches(
    shims: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    log, env = shims
    env = {k: v for k, v in env.items() if k not in ("WORKER_HOSTS", "BROWSER_WORKER_HOST")}
    result = run("seed_single_host.sh", str(_data_root(tmp_path)), env=env | {"FAKE_PENDING": "0"})
    expect(result.returncode == 0, result.stdout + result.stderr)
    lines = calls(log)
    compose = "docker compose -f docker-compose.yml -f compose.prod.yml -f compose.single.yml"
    expect(all(ln.startswith("ssh root@10.0.1.10 ") and compose in ln for ln in lines), lines)
    ship = first_index(
        lines,
        "run --rm --no-deps -T --user root --entrypoint sh worker -c 'tar -xz -C /var/lib/infraque/data",
    )
    load = first_index(lines, "worker python -m infra.scheduler.bootstrap load")
    drain = first_index(lines, "exec -T postgres psql")
    context = first_index(lines, "worker python -m infra.scheduler.bootstrap context")
    fetch = first_index(lines, "worker python -m infra.scheduler.bootstrap fetch")
    expect([ship, load, drain, context, fetch] == sorted([ship, load, drain, context, fetch]), lines)
    # The context load is waited for too, before the fetches start.
    drains = [i for i, ln in enumerate(lines) if "exec -T postgres psql" in ln]
    expect(any(context < i < fetch for i in drains), lines)
    expect("'match_tick', 'context_load'" in lines[drain], lines[drain])
    expect(not any(" web " in ln for ln in lines), "the worker image does all of it")
    expect("chown -R appuser:appuser /var/lib/infraque/data" in lines[ship], lines[ship])


def test_seed_stops_when_the_loads_do_not_drain(
    shims: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    log, env = shims
    env = env | {"FAKE_PENDING": "3", "SEED_DRAIN_TIMEOUT_SECONDS": "0", "SEED_DRAIN_INTERVAL_SECONDS": "0"}
    result = run("seed_single_host.sh", str(_data_root(tmp_path)), env=env)
    expect(result.returncode != 0 and "still pending" in result.stderr, result.stderr)
    expect(not any("bootstrap context" in ln for ln in calls(log)), "context must wait for the loads")


def test_seed_refuses_a_data_root_with_nothing_to_ship(
    shims: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    log, env = shims
    for root in (tmp_path / "absent", tmp_path):
        result = run("seed_single_host.sh", str(root), env=env)
        expect(result.returncode != 0, f"{root}: {result.stdout}")
    expect(calls(log) == [], "nothing may reach the host")
