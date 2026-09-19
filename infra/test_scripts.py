"""infra/scripts/*.sh: syntax (`bash -n`) for every script, and behavioural runs of deploy.sh,
rollback.sh and backup.sh against PATH shims that record every ssh/scp/sops/aws/pg_dump call
(no network, no Docker, no real key anywhere). Checks use `expect` instead of bare `assert`
(ruff S101, see infra/scheduler/test_jobs.py)."""

from __future__ import annotations

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
cat > /dev/null  # swallow piped stdin (secrets, tokens)
case "$*" in
  *"cat /opt/infraque/current-tag"*) printf '%s\n' "${FAKE_PREVIOUS_TAG:-}" ;;
  *"docker inspect"*) printf '%s\n' "${FAKE_HEALTH:-healthy}" ;;
esac
exit 0
"""
LOGGING_SHIM = (
    '#!/usr/bin/env bash\nprintf \'{name} %s\\n\' "$*" >> "$FAKE_LOG"\ncat > /dev/null 2>&1 || true\nexit 0\n'
)
SOPS_SHIM = (
    "#!/usr/bin/env bash\nprintf 'sops %s\\n' \"$*\" >> \"$FAKE_LOG\"\necho 'DOMAIN=example.test'\nexit 0\n"
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
    up_app = first_index(lines, "up -d --no-build caddy api web")
    health = first_index(lines, "docker inspect -f '{{.State.Health.Status}}'")
    health_url = first_index(lines, "curl -sf --max-time 5 http://api:8000/v1/health")
    up_worker = first_index(lines, "up -d --no-build worker")
    up_browser = first_index(lines, "up -d --no-build browser-worker")
    up_scheduler = first_index(lines, "up -d --no-build scheduler")
    record = first_index(lines, "> /opt/infraque/current-tag")
    order = [sync_app, secrets_app, pull, stop_worker, stop_scheduler, migrate, up_app, health, health_url]
    order += [up_worker, up_browser, up_scheduler, record]
    expect(order == sorted(order), f"deploy steps out of order: {order}\n" + "\n".join(lines))
    sops_call = "--input-type yaml --output-type dotenv infra/sops/secrets.staging.enc.yaml"
    expect(any(sops_call in ln for ln in lines), "sops must emit dotenv lines")
    expect(
        sum("cat > /opt/infraque/secrets/.env" in ln for ln in lines) == 4, "secrets must reach all 4 hosts"
    )
    expect(sum("scp " in ln and "backup.sh" in ln for ln in lines) == 4, "backup.sh must reach all 4 hosts")
    expect("| staging | sha-abc1234 |" in pathlib.Path(env["DEPLOY_LOG"]).read_text(), "deploy log row")


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
